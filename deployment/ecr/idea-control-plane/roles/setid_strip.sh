#!/bin/bash
#
# Clear set-user and set-group id bits, except pbs_iff's (batch client authentication) and sudo's
# (directory sudoers on the login node). Every role starts as root and drops privileges itself,
# so other set-id files only widen what a user with a shell can run.
#
#   setid_strip.sh --build /     the image build: walk /, record what was cleared in IMAGE_LIST,
#                                and fail if it cannot be walked or any bit is left
#   setid_strip.sh --image-list  after a site hook: clear the bit again only on the files the
#                                image shipped set-id (IMAGE_LIST), where a package update by the
#                                hook put it back; tools the hook installed keep their modes
#   setid_strip.sh DIR...        at start (the scheduler, after PBS starts): walk DIR, never shared
#                                storage (-xdev, and a DIR on a network or cluster file system or
#                                one whose file system cannot be named is skipped)
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
IMAGE_LIST="${IDEA_SETID_IMAGE_LIST:-/opt/idea/setid-cleared.list}"

keep=(! -path /usr/bin/sudo ! -path /opt/pbs/sbin/pbs_iff)

if [[ "${1:-}" == --image-list ]]; then
  [[ -f "${IMAGE_LIST}" ]] || exit 0
  while IFS= read -r file; do
    if [[ -f "${file}" && ( -u "${file}" || -g "${file}" ) ]]; then
      chmod ug-s "${file}"
      echo "cleared set-id bits again: ${file}"
    fi
  done < "${IMAGE_LIST}"
  exit 0
fi

build=0
if [[ "${1:-}" == --build ]]; then
  build=1
  shift
fi
roots=("$@")
(( ${#roots[@]} )) || { echo "usage: setid_strip.sh --build DIR | --image-list | DIR..." >&2; exit 2; }
walk=()
for root in "${roots[@]}"; do
  [[ -d "${root}" ]] || continue
  # GNU stat
  if ! fs="$(stat -f -c %T "${root}" 2>/dev/null)"; then
    (( build )) && { echo "cannot name the file system of ${root}; the image build cannot be checked" >&2; exit 1; }
    echo "not walking ${root}: file system unknown"
    continue
  fi
  case "${fs}" in
    nfs*|lustre|fuse*|efs|smb*|cifs)
      # a rootless builder's fuse-overlayfs root is the image itself: the build walks it anyway
      if (( ! build )); then
        echo "not walking ${root}: it is on shared storage"
        continue
      fi ;;
  esac
  walk+=("${root}")
done
(( ${#walk[@]} )) || exit 0
cleared="$(find "${walk[@]}" -xdev -type f -perm /6000 "${keep[@]}" -print -exec chmod ug-s {} +)"
if [[ -n "${cleared}" ]]; then
  printf 'cleared set-id bits:\n%s\n' "${cleared}"
  if (( build )); then printf '%s\n' "${cleared}" >> "${IMAGE_LIST}"; fi
fi
left="$(find "${walk[@]}" -xdev -type f -perm /6000 "${keep[@]}")"
if [[ -n "${left}" ]]; then
  printf 'set-id bits remain:\n%s\n' "${left}" >&2
  exit 1
fi
