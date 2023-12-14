# Configure Floating License resources

In this page, we will see how IDEA manages job and capacity provisioning based on license availabilities.

{% hint style="info" %}
**Example configuration**

Test settings used for all examples:

* License Server Hostname:`lic1.idea-licenses.internal`
* License Server port: `1999`
* License Daemon port: `1998`
* Feature to check: `ccmppower`
{% endhint %}

## Firewall Configuration <a href="#firewall-configuration" id="firewall-configuration"></a>

Depending your configuration, you may need to edit the security groups to allow traffic to/from your license server.

{% hint style="warning" %}
FlexLM configure two ports for each application (DAEMON and SERVER ports). You need to allow both of them.
{% endhint %}

### **Allow traffic from your license server IP to IDEA**

Navigate to EC2 console then click "**Security Groups**" and safe-list the license server IP(s) for the two ports on the following security groups:

* \<CLUSTER\_NAME>-scheduler-security-group
* \<CLUSTER\_NAME>-scheduler-compute-node-security-group
* \<CLUSTER\_NAME>-vdc-dcv-host-security-group
* \<CLUSTER\_NAME>-cluster-manager-security-group

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-29 at 10.51.58 AM.png" alt=""><figcaption><p>Example of IP/Ports safelist assuming license server IP is 10.0.15.8</p></figcaption></figure>

### **Allow traffic from IDEA to your license server**

Since FlexLM use client/server protocol, you will need to authorize traffic coming from IDEA to your license servers for both SERVER and DAEMON ports. Depending on your IDEA setup, you will need to safe-list:

* Standard Deployment:
  * NAT Gateway IP(s) associated to your IDEA environment ("**VPC console**" > "**NAT** **Gateways**")
* No Outgoing Internet Deployment:
  * Scheduler/Cluster Manager/VDC IP ranges&#x20;

## Upload your lmutil <a href="#upload-your-lmutil" id="upload-your-lmutil"></a>

lmutil binary is not included with IDEA. You are required to upload it manually and update `/apps/<CLUSTER_NAME>/scheduler/scripts/license_check.py` with the location of your file. To avoid permission issue, it's recommended to create a copy of the file and move it to another location (as /apps/\<IDEA\_CLUSTER> is protected). General recommendation is to create `/apps/utils` and copy the license\_check.py on this location.

```
arg = parser.parse_args()
lmstat_path = "PATH_TO_LMUTIL"
if lmstat_path == "PATH_TO_LMUTIL":
    print('Please specify a link to your lmutil binary (edit line 30 of this file')
    sys.exit(1)
```

{% hint style="info" %}
You do not need to install FlexLM server manager. Only `lmutil` binary is required.
{% endhint %}

{% hint style="warning" %}
lmutil and RHEL based distro

FlexLM may requires 32 bits lib depending your system. If launching `lmutil` returns an `ELF` version mismatch, simply install `yum install redhat-lsb` (or equivalent
{% endhint %}

## How to retrieve number of licenses available <a href="#how-to-retrieve-number-of-licenses-available" id="how-to-retrieve-number-of-licenses-available"></a>

IDEA includes a script (`/apps/<IDEA-CLUSTER>/scheduler/scripts/license_check.py`) which output the number of FlexLM available for a given feature. This script takes the following arguments:

* \-s: The license server hostname
* \-p: The port used by your flexlm deamon
* \-f: The feature name (case sensitive)

{% hint style="info" %}
To avoid permission issue, it's recommended to create a copy of the file and move it to another location (as /apps/\<IDEA\_CLUSTER> is protected). General recommendation is to create `/apps/utils` and copy the license\_check.py on this location.
{% endhint %}

Let say you have 30 ccmp licenses and 4 are currently in use. The command below will list how many licenses are currently available to use for your jobs:

```bash
python /apps/utils/license_check.py -s lic1.idea-licenses.internal -p 1999 -f ccmppower
26
```

{% hint style="info" %}
`license_check.py` is simply a `lmutil` wrapper. You can get the same output by running the command below and adding some regex validations. You can edit the script to match your own requirements if needed

`lmutil lmstat -a -c 1999@lic1.idea-licenses.internal | grep "Users of ccmppower:"`
{% endhint %}

## Integration with IDEA <a href="#integration-with-scale-out-computing-on-aws" id="integration-with-scale-out-computing-on-aws"></a>

{% hint style="warning" %}
The name of the resource **must** be `*_lic_*`. We recommend using `<application>_lic_<feature_name>`
{% endhint %}

Navigate to  "**Licenses**" under "**Scale-Out Computing**" section on your IDEA web interface then click "**Create License Resource**" to fill out the form below.&#x20;

In our example, the availability check script command is:

```
python /apps/utils/license_check.py --server lic1.idea-licenses.internal \
  --port 1999 \ 
  --feature ccmppower
```

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-29 at 2.56.07 PM.png" alt=""><figcaption><p>Add your license resource information</p></figcaption></figure>

Once ready, click "**Next**" and IDEA will validate your configuration.&#x20;

{% hint style="warning" %}
If you are getting a "Permission Denied", ensure the availability check script is in a location your client can access (e.g: /apps/)
{% endhint %}

Step2 will requires you to configure the scheduler manually. Follow the instructions mentioned on the page:

* Connect to the Scheduler EC2 instance&#x20;
* Update your `resourcedef`
* Update your `sched_config`&#x20;
* &#x20;Restart OpenPBS

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-29 at 2.59.16 PM.png" alt=""><figcaption><p>Follow the instructions to finalize your configuration</p></figcaption></figure>

Once done, validate your configuration by clicking "**Create License Resource**". If your configuration is correct, you will now see the name of your License Resource as well as a real-time count of the number of licenses available.

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-29 at 3.03.20 PM.png" alt=""><figcaption><p>Your License resource (starccm_lic_ccmppower) is now ready to use</p></figcaption></figure>

IDEA checks the configuration on your behalf and will return an error if you haven't configured your `resourcedef` / `sched_config` correctly.

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-29 at 3.04.46 PM.png" alt=""><figcaption><p>Example of invalid configuration</p></figcaption></figure>

## Submit a job with License Restrictions <a href="#test" id="test"></a>

Use `-l <resource_name>=<number_of_licenses>`to submit a job with license restriction

**Example**:

&#x20;`qsub -l starccm_lic_ccmppower=5 -- /bin/sleep 600`

IDEA will ensures capacity will be provisioned only if there is at least 5 StarCCM+ ccmppower licenses available.

You can combine multiple license restrictions if needed

`qsub -l starccm_lic_ccmppower=5 -l starccm_lic_ccmpsuite=1 -- -/bin/sleep 600`

**Example if you are using a job script:**

```
#PBS -N <jobname>
#PBS -q <queue>
#PBS -l starccm_lic_ccmppower=5 
/bin/sleep 600
```
