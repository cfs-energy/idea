---
description: How to share your result files on IDEA
---

# Data Sharing Between Users

* Navigate to your IDEA cluster > account settings

<figure><img src="../../.gitbook/assets/f445dc5a-a54b-44ad-bcd4-2871ad3dd3e3.png" alt=""><figcaption></figcaption></figure>

* Click on the actions button > add user to my group

<figure><img src="../../.gitbook/assets/0216944b-9791-4668-9184-d4374dba78b0.png" alt=""><figcaption></figcaption></figure>

* Click the plus button to add users > type the usernames you want to share with

<figure><img src="../../.gitbook/assets/eb50786d-c7e7-4e68-940b-717a2fbdb242.png" alt=""><figcaption></figcaption></figure>

<figure><img src="../../.gitbook/assets/3d9e9f21-a24a-45eb-b34d-21741f773cec.png" alt=""><figcaption></figcaption></figure>

* Verify usernames have been added and are spelled correctly

<figure><img src="../../.gitbook/assets/6509b2be-19e1-423f-9a16-150c43dcdc83.png" alt=""><figcaption></figcaption></figure>

* Open SSH client > connect to HPC IDEA Cluster. Note: If you haven't set this up yet, follow the tutorial here: [https://cidea.cfsenergy.com/#/home/ssh-access](https://cidea.cfsenergy.com/#/home/ssh-access)

<figure><img src="../../.gitbook/assets/00fe6a60-2b44-4820-a641-4b642408f40f.png" alt=""><figcaption></figcaption></figure>

&#x20;

* Ensure you are in your username's directory (use "pwd")

<figure><img src="../../.gitbook/assets/a6db70b7-3ec8-41b2-96c7-0031302a3fc5.png" alt=""><figcaption></figcaption></figure>

* Type "chmod g+rx ." (no quotes, don't forget the period) to update permissions for your username's directory

<figure><img src="../../.gitbook/assets/6c46be0c-0566-4819-aa34-9adf6d445fad.png" alt=""><figcaption></figcaption></figure>

* Type "cd .." (no quotes) to navigate to the next level up in the directory hierarchy

<figure><img src="../../.gitbook/assets/6660c089-1bf7-4686-af8e-53ac7ffb469c.png" alt=""><figcaption></figcaption></figure>

* Type "ls -al" (no quotes) and verify your directory has "drwxr-x---" permissions

<figure><img src="../../.gitbook/assets/0824a414-8dbd-4f06-80e9-2eb16d95f9e4.png" alt=""><figcaption></figcaption></figure>

Those who you've shared with should now be able to download files from your directory!

\
