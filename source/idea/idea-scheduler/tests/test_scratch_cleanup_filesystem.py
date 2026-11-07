"""The descriptor walk: links are never followed and a swapped path is never removed."""

import os
from datetime import datetime, timezone
import pytest

from ideascheduler.app.scratch_cleanup import filesystem as module
from ideascheduler.app.scratch_cleanup.filesystem import (
    FolderIdentity,
    FolderInUse,
    NotASafeTarget,
    RemovalProgress,
    identity_of,
    inspect_folder,
    list_directory,
    lustre_mount_problem,
    open_child_dir,
    remove_folder,
)


ANY_TIME = {'not_before': 0.0, 'not_after': 2.0**40, 'dirs_not_after': 2.0**40}


@pytest.fixture()
def root(tmp_path):
    mount = tmp_path / 'mount'
    mount.mkdir()
    fd = os.open(mount, os.O_RDONLY)
    yield mount, fd
    os.close(fd)


def tree(folder):
    (folder / 'sub' / 'deep').mkdir(parents=True)
    (folder / 'a.dat').write_bytes(b'a' * 5000)
    (folder / 'sub' / 'deep' / 'b.dat').write_bytes(b'b' * 5000)


def nothing():
    pass


def inspect(fd, device=None, uid=None, budget=1000):
    return inspect_folder(
        fd,
        os.fstat(fd).st_dev if device is None else device,
        os.getuid() if uid is None else uid,
        budget,
    )


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '../mount', '/etc'])
def test_names_that_could_leave_the_level_are_refused(root, name):
    with pytest.raises(NotASafeTarget):
        open_child_dir(root[1], name)


def test_a_link_to_a_directory_is_not_opened(root, tmp_path):
    mount, fd = root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (mount / 'link').symlink_to(outside)
    with pytest.raises(NotASafeTarget):
        open_child_dir(fd, 'link')


def test_a_file_is_not_opened_as_a_folder(root):
    mount, fd = root
    (mount / 'file').write_text('x')
    with pytest.raises(NotASafeTarget):
        open_child_dir(fd, 'file')


def test_listing_reports_a_link_as_not_a_directory(root, tmp_path):
    mount, fd = root
    (mount / 'real').mkdir()
    (mount / 'link').symlink_to(mount / 'real')
    assert dict(list_directory(fd)) == {'real': True, 'link': False}


def test_inspect_counts_size_and_newest_change(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    tree(folder)
    folder_fd = open_child_dir(fd, 'job')
    try:
        found = inspect(folder_fd)
    finally:
        os.close(folder_fd)
    assert found.entries == 4
    assert found.size_bytes >= 10000
    assert abs((datetime.now(timezone.utc) - found.newest_change).total_seconds()) < 60
    assert not (found.other_owner or found.other_mount or found.over_budget)


def test_inspect_reports_the_latest_read_of_a_file_only(root):
    mount, fd = root
    folder = mount / 'job'
    (folder / 'sub').mkdir(parents=True)
    (folder / 'data').write_text('x')
    os.utime(folder / 'data', (5000000000, 1000))
    os.utime(folder / 'sub', (9000000000, 1000))
    folder_fd = open_child_dir(fd, 'job')
    try:
        found = inspect(folder_fd)
    finally:
        os.close(folder_fd)
    # a directory's atime moves whenever it is listed, this walk included
    assert found.newest_access.timestamp() == 5000000000


def test_inspect_does_not_follow_a_link_out(root, tmp_path):
    mount, fd = root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'huge').write_bytes(b'x' * 100000)
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'escape').symlink_to(outside)
    folder_fd = open_child_dir(fd, 'job')
    try:
        found = inspect(folder_fd)
    finally:
        os.close(folder_fd)
    assert found.entries == 1
    assert found.size_bytes < 100000


def test_inspect_notices_another_owner(root):
    mount, fd = root
    (mount / 'job').mkdir()
    folder_fd = open_child_dir(fd, 'job')
    try:
        assert inspect(folder_fd, uid=os.getuid() + 1).other_owner
    finally:
        os.close(folder_fd)


def test_inspect_notices_another_file_system(root):
    mount, fd = root
    folder = mount / 'job'
    (folder / 'mounted').mkdir(parents=True)
    folder_fd = open_child_dir(fd, 'job')
    try:
        assert inspect(folder_fd, device=os.fstat(fd).st_dev + 1).other_mount
    finally:
        os.close(folder_fd)


def test_inspect_gives_up_over_budget(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    for index in range(5):
        (folder / str(index)).write_text('x')
    folder_fd = open_child_dir(fd, 'job')
    try:
        assert inspect(folder_fd, budget=3).over_budget
    finally:
        os.close(folder_fd)


def test_inspect_too_deep_counts_as_too_large(root, monkeypatch):
    monkeypatch.setattr(module, 'MAX_DEPTH', 2)
    mount, fd = root
    (mount / 'job' / 'a' / 'b' / 'c').mkdir(parents=True)
    folder_fd = open_child_dir(fd, 'job')
    try:
        assert inspect(folder_fd).over_budget
    finally:
        os.close(folder_fd)


def open_for_removal(fd, name):
    folder_fd = open_child_dir(fd, name)
    return folder_fd, identity_of(folder_fd)


def test_remove_takes_only_the_folder(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    tree(folder)
    (mount / 'other').mkdir()
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        assert remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME) == 5
    finally:
        os.close(folder_fd)
    assert sorted(os.listdir(mount)) == ['other']


def test_a_link_inside_goes_and_what_it_points_at_stays(root, tmp_path):
    mount, fd = root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'to-dir').symlink_to(outside)
    (folder / 'to-file').symlink_to(outside / 'precious')
    (folder / 'to-root').symlink_to('/')
    (folder / 'dangling').symlink_to('/no/such')
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert not folder.exists()
    assert (outside / 'precious').read_text() == 'keep'


def test_a_hard_link_removes_only_this_name(root, tmp_path):
    mount, fd = root
    outside = tmp_path / 'outside.dat'
    outside.write_text('keep')
    folder = mount / 'job'
    folder.mkdir()
    os.link(outside, folder / 'linked')
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert outside.read_text() == 'keep'


def test_a_descriptor_for_another_folder_is_refused(root):
    mount, fd = root
    (mount / 'job').mkdir()
    (mount / 'other').mkdir()
    folder_fd = open_child_dir(fd, 'other')
    wrong = identity_of(open_keep(fd, 'job'))
    try:
        with pytest.raises(NotASafeTarget):
            remove_folder(fd, 'other', folder_fd, wrong, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (mount / 'other').exists()


def open_keep(fd, name, _kept=[]):
    opened = open_child_dir(fd, name)
    _kept.append(opened)
    return opened


def test_a_name_swapped_for_a_link_during_removal_is_not_removed(root, tmp_path):
    mount, fd = root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'a').write_text('x')
    folder_fd, identity = open_for_removal(fd, 'job')
    os.rename(folder, mount / 'moved')
    (mount / 'job').symlink_to(outside)
    try:
        with pytest.raises(NotASafeTarget):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    # the walk stayed on the open folder and the link was left, as was its target
    assert (outside / 'precious').read_text() == 'keep'
    assert (mount / 'job').is_symlink()


def test_a_subfolder_swapped_for_a_link_is_not_followed(root, tmp_path, monkeypatch):
    mount, fd = root
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    folder = mount / 'job'
    (folder / 'sub').mkdir(parents=True)
    real_listing = module.list_directory

    def swapping(listed_fd):
        entries = list(real_listing(listed_fd))
        if (
            os.fstat(listed_fd).st_ino == os.stat(folder).st_ino
            and (folder / 'sub').is_dir()
        ):
            os.rmdir(folder / 'sub')
            (folder / 'sub').symlink_to(outside)
        return iter(entries)

    monkeypatch.setattr(module, 'list_directory', swapping)
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        with pytest.raises(NotASafeTarget):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (outside / 'precious').read_text() == 'keep'


def replacement_tree(mount):
    """a checked subfolder, and a directory that takes its place once it has been checked"""
    folder = mount / 'job'
    (folder / 'sub').mkdir(parents=True)
    (folder / 'sub' / 'own.dat').write_text('own')
    other = mount / 'someone-elses'
    other.mkdir()
    (other / 'precious').write_text('keep')
    return folder, other


def swap_on_open(monkeypatch, folder, other):
    real_open = module.open_child_dir
    swapped = []

    def swapping(parent_fd, name):
        if name == 'sub' and not swapped:
            swapped.append(True)
            os.rename(folder / 'sub', folder.parent / 'set-aside')
            os.rename(other, folder / 'sub')
        return real_open(parent_fd, name)

    monkeypatch.setattr(module, 'open_child_dir', swapping)


def test_a_replacement_directory_after_the_check_is_not_traversed(root, monkeypatch):
    mount, fd = root
    folder, other = replacement_tree(mount)
    folder_fd, identity = open_for_removal(fd, 'job')
    swap_on_open(monkeypatch, folder, other)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (folder / 'sub' / 'precious').read_text() == 'keep'
    assert (mount / 'set-aside' / 'own.dat').read_text() == 'own'


def test_a_replacement_directory_during_inspection_is_not_measured(root, monkeypatch):
    mount, fd = root
    folder, other = replacement_tree(mount)
    folder_fd = open_child_dir(fd, 'job')
    swap_on_open(monkeypatch, folder, other)
    try:
        assert inspect(folder_fd).unreadable
    finally:
        os.close(folder_fd)


def test_a_subfolder_given_to_another_owner_after_the_check_stops_it(root, monkeypatch):
    mount, fd = root
    folder, _ = replacement_tree(mount)
    folder_fd, identity = open_for_removal(fd, 'job')
    real_fstat = os.fstat
    sub_inode = os.stat(folder / 'sub').st_ino

    def fstat(target):
        info = real_fstat(target)
        if info.st_ino != sub_inode:
            return info
        values = list(info)
        values[4] = info.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(module.os, 'fstat', fstat)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (folder / 'sub' / 'own.dat').exists()


def nested_files(folder, count=5):
    (folder / 'sub' / 'deep').mkdir(parents=True)
    for index in range(count):
        (folder / 'sub' / 'deep' / f'{index}.dat').write_text('x')


@pytest.mark.parametrize('moved', ['sub/deep', 'sub'])
def test_a_folder_moved_out_while_being_emptied_loses_nothing_more(
    root, monkeypatch, moved
):
    # the descriptor still works on the moved folder: its name is what shows the move, and no
    # file goes after it, not even within the hundred operations between the wider checks
    mount, fd = root
    folder = mount / 'job'
    nested_files(folder)
    folder_fd, identity = open_for_removal(fd, 'job')
    real_unlink = os.unlink
    gone = []

    def unlink(name, dir_fd=None):
        real_unlink(name, dir_fd=dir_fd)
        gone.append(name)
        if len(gone) == 1:
            os.rename(folder / moved, mount / 'saved')

    monkeypatch.setattr(module.os, 'unlink', unlink)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert len(gone) == 1


def test_a_folder_moved_before_anything_in_it_goes_loses_nothing(root, monkeypatch):
    mount, fd = root
    folder = mount / 'job'
    nested_files(folder)
    folder_fd, identity = open_for_removal(fd, 'job')
    real_listing = module.list_directory
    moved = []

    def listing(listed_fd):
        entries = list(real_listing(listed_fd))
        if (
            not moved
            and os.fstat(listed_fd).st_ino == os.stat(folder / 'sub' / 'deep').st_ino
        ):
            moved.append(True)
            os.rename(folder / 'sub', mount / 'saved')
        return iter(entries)

    monkeypatch.setattr(module, 'list_directory', listing)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert len(os.listdir(mount / 'saved' / 'deep')) == 5


def test_a_folder_put_back_under_another_parent_is_not_followed(root, monkeypatch):
    # the top of the folder moved: nothing inside it goes, and the owner has saved it
    mount, fd = root
    folder = mount / 'job'
    nested_files(folder)
    folder_fd, identity = open_for_removal(fd, 'job')
    real_listing = module.list_directory
    moved = []

    def listing(listed_fd):
        entries = list(real_listing(listed_fd))
        if not moved:
            moved.append(True)
            os.rename(folder, mount / 'rescued')
        return iter(entries)

    monkeypatch.setattr(module, 'list_directory', listing)
    try:
        with pytest.raises((FolderInUse, NotASafeTarget)):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert len(os.listdir(mount / 'rescued' / 'sub' / 'deep')) == 5


def test_the_checks_cost_a_bounded_number_of_stats_per_entry(root, monkeypatch):
    mount, fd = root
    folder = mount / 'job'
    (folder / 'a' / 'b').mkdir(parents=True)
    count = 2000
    for index in range(count):
        (folder / 'a' / 'b' / f'{index}').touch()
    real_stat = os.stat
    stats = []

    def counted(*args, **kwargs):
        stats.append(1)
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(module.os, 'stat', counted)
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    # one for the file itself, and one for each level up to the folder's own name
    assert len(stats) <= count * 5 + 50


def test_an_empty_replacement_is_not_removed_in_place_of_the_folder_emptied(
    root, monkeypatch
):
    mount, fd = root
    folder = mount / 'job'
    (folder / 'sub').mkdir(parents=True)
    other = mount / 'someone-elses'
    other.mkdir()
    swapped = []
    real_close = os.close
    sub_fds = []
    real_enter = module.open_child_dir

    def enter(parent_fd, name):
        opened = real_enter(parent_fd, name)
        if name == 'sub':
            sub_fds.append(opened)
        return opened

    def close(target):
        real_close(target)
        if target in sub_fds and not swapped:
            swapped.append(True)
            os.rename(folder / 'sub', mount / 'set-aside')
            os.rename(other, folder / 'sub')

    monkeypatch.setattr(module, 'open_child_dir', enter)
    monkeypatch.setattr(module.os, 'close', close)
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        real_close(folder_fd)
    assert (folder / 'sub').is_dir()


def test_a_write_during_removal_stops_it(root, monkeypatch):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'a').write_text('x')
    real_unlink = os.unlink

    def writing(name, dir_fd=None):
        real_unlink(name, dir_fd=dir_fd)
        (folder / 'written-during-removal').write_text('y')

    monkeypatch.setattr(module.os, 'unlink', writing)
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, identity, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (folder / 'written-during-removal').exists()


def test_something_owned_by_someone_else_stops_removal(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'theirs').write_text('keep')
    folder_fd = open_child_dir(fd, 'job')
    actual = identity_of(folder_fd)
    # the run checked the folder as belonging to another uid than what is inside now
    expected = FolderIdentity(actual.device, actual.inode, actual.uid + 1)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(fd, 'job', folder_fd, expected, nothing, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert (folder / 'theirs').read_text() == 'keep'


@pytest.mark.parametrize('layout', ['one_file_each', 'directories_only'], ids=str)
def test_no_shape_of_tree_avoids_the_checks(root, layout):
    mount, fd = root
    folder = mount / 'job'
    for index in range(1200):
        sub = folder / f's{index}'
        sub.mkdir(parents=True)
        if layout == 'one_file_each':
            (sub / 'f').touch()
    calls = []
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        remove_folder(
            fd, 'job', folder_fd, identity, lambda: calls.append(1), **ANY_TIME
        )
    finally:
        os.close(folder_fd)
    assert calls


def test_progress_survives_an_exception(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    for index in range(1500):
        (folder / str(index)).touch()

    class Stop(Exception):
        pass

    def stop():
        raise Stop()

    progress = RemovalProgress()
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        with pytest.raises(Stop):
            remove_folder(
                fd, 'job', folder_fd, identity, stop, progress=progress, **ANY_TIME
            )
    finally:
        os.close(folder_fd)
    assert progress.entries == 1500 - len(os.listdir(folder))
    assert progress.entries > 0


def test_checks_run_at_least_every_hundred_operations(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    for index in range(1000):
        (folder / str(index)).touch()
    calls = []
    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        remove_folder(
            fd, 'job', folder_fd, identity, lambda: calls.append(1), **ANY_TIME
        )
    finally:
        os.close(folder_fd)
    assert len(calls) >= 10


def test_moving_the_folder_away_stops_a_removal_in_progress(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    for index in range(2500):
        (folder / str(index)).touch()
    folder_fd, identity = open_for_removal(fd, 'job')

    def owner_rescues():
        if folder.exists():
            os.rename(folder, mount / 'rescued')

    try:
        with pytest.raises(NotASafeTarget):
            remove_folder(fd, 'job', folder_fd, identity, owner_rescues, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert len(os.listdir(mount / 'rescued')) == 2500 - module.CHECK_EVERY_OPERATIONS


def test_inspect_reports_the_oldest_ctime_and_newest_file(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    (folder / 'a').write_text('x')
    os.utime(folder / 'a', (1, 1))
    folder_fd = open_child_dir(fd, 'job')
    try:
        found = inspect(folder_fd)
    finally:
        os.close(folder_fd)
    # backdating sets mtime, and ctime keeps the truth
    assert found.oldest_change.year >= 2020
    assert found.newest_file_change is not None


def test_keep_going_can_stop_a_long_removal_where_it_is(root):
    mount, fd = root
    folder = mount / 'job'
    folder.mkdir()
    for index in range(2500):
        (folder / str(index)).touch()

    class Stop(Exception):
        pass

    def stop():
        raise Stop()

    folder_fd, identity = open_for_removal(fd, 'job')
    try:
        with pytest.raises(Stop):
            remove_folder(fd, 'job', folder_fd, identity, stop, **ANY_TIME)
    finally:
        os.close(folder_fd)
    assert folder.exists()
    assert len(os.listdir(folder)) == 2500 - module.CHECK_EVERY_OPERATIONS


def test_remove_refuses_another_file_system_inside(root, monkeypatch):
    mount, fd = root
    folder = mount / 'job'
    (folder / 'mounted').mkdir(parents=True)
    (folder / 'mounted' / 'data').write_text('keep')
    folder_fd = open_child_dir(fd, 'job')
    identity = identity_of(folder_fd)
    real_fstat = os.fstat
    mounted_inode = os.stat(folder / 'mounted').st_ino

    def fstat(target):
        info = real_fstat(target)
        if info.st_ino != mounted_inode:
            return info
        values = list(info)
        values[2] = info.st_dev + 1
        return os.stat_result(values)

    monkeypatch.setattr(module.os, 'fstat', fstat)
    try:
        with pytest.raises(FolderInUse):
            remove_folder(
                fd,
                'job',
                folder_fd,
                FolderIdentity(identity.device, identity.inode, identity.uid),
                nothing,
                **ANY_TIME,
            )
    finally:
        os.close(folder_fd)
    assert (folder / 'mounted' / 'data').read_text() == 'keep'


MOUNTINFO = (
    '22 1 259:1 / / rw,noatime - xfs /dev/nvme0n1p1 rw\n'
    '40 22 0:40 / /lustre rw,noatime - lustre 10.0.0.9@tcp:/abcdefgh rw,flock\n'
    '41 22 0:41 / /empty rw - xfs /dev/nvme0n1p1 rw\n'
)


def problem(tmp_path, mount_dir, mount_name='abcdefgh', text=MOUNTINFO):
    info = tmp_path / 'mountinfo'
    info.write_text(text)
    return lustre_mount_problem(mount_dir, mount_name, str(info))


def test_the_configured_lustre_mount_passes(tmp_path):
    assert problem(tmp_path, '/lustre') is None
    assert problem(tmp_path, '/lustre/') is None


def test_an_unmounted_path_is_refused(tmp_path):
    assert 'not mounted' in problem(tmp_path, '/fsx')


def test_a_path_on_the_local_disk_is_refused(tmp_path):
    assert 'not Lustre' in problem(tmp_path, '/empty')


def test_another_lustre_file_system_is_refused(tmp_path):
    assert 'not the configured' in problem(tmp_path, '/lustre', mount_name='other')


def test_a_later_mount_over_the_path_decides(tmp_path):
    over = MOUNTINFO + '50 40 0:50 / /lustre rw - tmpfs tmpfs rw\n'
    assert 'not Lustre' in problem(tmp_path, '/lustre', text=over)


def test_an_unreadable_mount_table_is_refused(tmp_path):
    assert lustre_mount_problem('/lustre', 'x', str(tmp_path / 'absent'))
