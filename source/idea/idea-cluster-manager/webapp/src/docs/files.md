## Files

Browse, edit and transfer files, manage favorites, or follow file output. Job submission and script actions open My jobs. Select **Upload files** to upload files or folders. Use **Favorites** for saved locations.

### Storage and deletion

Open **My costs > Storage** for home usage, top-level folder sizes, **Last changed**, **Oldest file**, and total bytes unchanged for 90 days. Ages use modification times, not last-read times. Measurements are cached for one hour and exclude symbolic links.

ONTAP quota reports show used space, files, limit and report time. When ONTAP storage is configured but no report exists for your account, My costs explains that the quota is unavailable. Without configured ONTAP storage, it shows no unavailable line.

In Files, select one folder and choose **Delete folder** from the toolbar, **Actions**, or the right-click menu. The confirmation shows size, file count, last change and measurement time. Type the folder name exactly to confirm permanent deletion of the folder and everything inside it.

Scans stop at 100,000 entries or ten seconds and limit directory depth to 128. Unreadable data also produces partial results. Partial sizes read **At least** and are lower bounds. Deletion stays disabled until a complete measurement and folder identity are available.

Only folders inside your home directory can be deleted this way. **Folder changed since it was measured** means the directory device or inode no longer matches. Close the confirmation and measure and review the folder again. This check does not detect every change to files inside the folder.
