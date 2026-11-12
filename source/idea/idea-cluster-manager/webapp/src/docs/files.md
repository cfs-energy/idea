## Files

Browse, edit and transfer files, manage favorites, or follow file output. Job submission and script actions open My jobs. Select **Upload files** to upload files or folders. Use **Favorites** for saved locations.

### Downloads

A folder or several files download as one zip, built on the server and removed once it has been sent. Building it is stopped after five minutes; for large folders use the commands on the **SSH access** page. Files that can't be read are left out, and the message says how many.

### Storage and deletion

Open **My costs > Storage** for home usage, top-level folder sizes, **Last changed**, **Oldest file**, and total bytes unchanged for 90 days. Ages use modification times, not last-read times. Measurements are cached for one hour and exclude symbolic links.

Your storage quota shows used space, files, and limit. If your quota can't be read, My costs says so.

In Files, select one folder and choose **Delete folder** from the toolbar, **Actions**, or the right-click menu. The confirmation shows size, file count, last change and measurement time. Type the folder name exactly to confirm permanent deletion of the folder and everything inside it.

Folder sizes come from the file system's own analytics when it is on for the volume: every folder you can open, largest first, with no file scan. Without it, a short scan lists your whole home folder and measures what it can in ten seconds or 100,000 entries, one scan at a time. Sizes that scan did not finish read **At least**, and folders it did not reach read **Not measured yet**. Deletion stays disabled until a complete measurement and folder identity are available; the confirmation gives the command to delete the folder from a terminal instead.

Only folders inside your home directory can be deleted this way. A delete that runs for five minutes is stopped, and what it removed stays removed. **Folder changed since it was measured** means the directory device or inode no longer matches. Close the confirmation and measure and review the folder again. This check does not detect every change to files inside the folder.
