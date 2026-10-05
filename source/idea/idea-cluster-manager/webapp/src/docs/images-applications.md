## Images and applications

Desktop images manages the desktop catalog and project access. Images shows the images desktops and jobs launch from. Submission forms manages job forms and scripts. Each view requires its existing module privileges.

### Images

**Managed images** has one row per operating system, architecture and GPU variant. IDEA rebuilds each image from the newest vendor image after an upgrade, on the monthly vendor check, or when you click **Refresh and validate**. It test-launches a desktop or job from the new image and switches new launches to it only if every check passes. Running desktops and jobs are never touched.

* **Current**: the image in use passed every check.
* **Baking**: a new image is being built and checked.
* **Failed**: a check failed. New launches stay on the image shown. Open **View log** or **Details** for the reason.
* **Waiting for capacity**: retries at the time shown.
* **Pinned**: not rebuilt or switched until you unpin it.

An image is baked at most once a day. **Force rebake** bakes it again. **Roll back** returns new launches to the previous validated image and pauses automatic updates for that row until you refresh it.

**Custom images** are builds you start yourself. They are not validated, and the managed refresh never changes them. A custom build that nothing uses is removed after 30 days by default.
