# My job is not starting

Jobs can stay in the Q state for extended period of time because of license restrictions or other applicationS specific requirements. Follow this guide if you think all conditions are met but your job is still stuck in the Q state.

## 1 - Verify if the capacity is being provisioned

&#x20;Verify if the capacity associated to the job is being provisioned by running the following command:

```
qstat -f <job_id> | grep select
```

* If `compute_node` value is set to `tbd` : Jobs is not eligible to run for the reasons mentioned above.&#x20;
* If `compute_node` value is set to `compute_node=idea-<CLUSTER>-compute-ondemand-<JOB_ID>`: In this case IDEA has triggered CloudFormation and the capacity is being provisioned

{% hint style="info" %}
You can login to AWS Console and navigate to the CloudFormation console to verify the CloudFormation stack associated to your job is in `CREATE_COMPLETE` state. If not, verify any potential errors via the `Events` tab.
{% endhint %}

## 2 - Verify the bootstrap logs for the compute node(s) being provisioned

If the capacity is being provisioned, the next thing to check is if there is no errors during the bootstrap sequence on the compute node(s) provisioned to run your job.&#x20;

To verify that, review logs located under `/apps/<CLUSTER>/scheduler/jobs/<JOB_ID>/`

You will find the boostrap & compute\_node logs for all EC2 instances being provisioned for your job:

<pre><code><strong>Bootstrap:
</strong><strong>/apps/&#x3C;CLUSTER>/scheduler/jobs/&#x3C;JOB_ID>/bootstrap/&#x3C;COMPUTE_NODE_JOB_ID>
</strong><strong>
</strong><strong>Compute Node Startup Logs:
</strong>/apps/&#x3C;CLUSTER>/scheduler/jobs/&#x3C;JOB_ID>/logs/&#x3C;INSTANCE_HOSTNAME>
</code></pre>

## 3 - Check if the compute node(s) is/are being registered on the scheduler

Verify if the compute node(s) are being registered correctly to the scheduler.&#x20;

Run `pbsnodes -a` and find the section specific to your job id (see example below for job 103)

```bash
p-10-110-6-141
     Mom = ip-10-110-6-141.us-east-2.compute.internal
     Port = 15002
     pbs_version = unavailable
     ntype = PBS
     state = state-unknown,down
     resources_available.anonymous_metrics = True
     resources_available.auto_scaling_group = idea-demo-compute-ondemand-103-AutoScalingComputeGroup-YXWJFA4XLGKQ
     resources_available.availability_zone = us-east-2c
     resources_available.base_os = rhel7
     resources_available.cluster_name = idea-demo
     resources_available.compute_node = idea-demo-compute-ondemand-103
     resources_available.efa_support = True
     resources_available.force_ri = False
     resources_available.fsx_lustre = False
     resources_available.host = ip-10-110-6-141
     resources_available.ht_support = False
     resources_available.instance_ami = ami-0c1c3220d0b1716d2
     resources_available.instance_id = i-02447d465fbe84723
     resources_available.instance_profile = AIPA6ERFY3V55DSVSI2OZ
     resources_available.instance_type = c5n.9xlarge
     resources_available.job_group = gf4ea4e11
     resources_available.job_id = 103
     resources_available.keep_ebs = False
     resources_available.keep_forever = False
     resources_available.launch_time = 1671017843
     resources_available.placement_group = False
     resources_available.provisioning_time = 1671017862089
     resources_available.queue_type = compute
     resources_available.root_size = 10gb
     resources_available.scaling_mode = single-job
     resources_available.scratch_iops = 0
     resources_available.scratch_size = 0gb
     resources_available.stack_id = arn:aws:cloudformation:us-east-2:<REDACTED>:stack/idea-demo-compute-ondemand-103/9caa5e10-7ba3-11ed-837d-060b0d4719a4
     resources_available.subnet_id = subnet-064c905368b057b68
     resources_available.system_metrics = False
     resources_available.tenancy = default
     resources_available.terminate_when_idle = 0
     resources_available.vnode = ip-10-110-6-141
     resources_assigned.accelerator_memory = 0kb
     resources_assigned.hbmem = 0kb
     resources_assigned.mem = 0kb
     resources_assigned.naccelerators = 0
     resources_assigned.ncpus = 0
     resources_assigned.vmem = 0kb
     queue = normal
     resv_enable = True
     sharing = default_shared
```

In this example, the host is still being configured as state is `state = state-unknown,down.` If that's the case, wait a little longer. Your host will be ready to accept job when `state = free` (see below)

```
ip-10-110-6-141
     Mom = ip-10-110-6-141.us-east-2.compute.internal
     ntype = PBS
     state = free
     pcpus = 18
     resources_available.anonymous_metrics = True
     resources_available.arch = linux
     resources_available.auto_scaling_group = idea-demo-compute-ondemand-103-AutoScalingComputeGroup-YXWJFA4XLGKQ
     resources_available.availability_zone = us-east-2c
     resources_available.base_os = rhel7
     resources_available.cluster_name = idea-demo
     resources_available.compute_node = idea-demo-compute-ondemand-103
     resources_available.efa_support = True
     resources_available.force_ri = False
     resources_available.fsx_lustre = False
     resources_available.host = ip-10-110-6-141
     resources_available.ht_support = False
     resources_available.instance_ami = ami-0c1c3220d0b1716d2
     resources_available.instance_id = i-02447d465fbe84723
     resources_available.instance_profile = AIPA6ERFY3V55DSVSI2OZ
     resources_available.instance_type = c5n.9xlarge
     resources_available.job_group = gf4ea4e11
     resources_available.job_id = 103
     resources_available.keep_ebs = False
     resources_available.keep_forever = False
     resources_available.launch_time = 1671017843
     resources_available.mem = 96640680kb
     resources_available.ncpus = 18
     resources_available.placement_group = False
     resources_available.provisioning_time = 1671017862089
     resources_available.queue_type = compute
     resources_available.root_size = 10gb
     resources_available.scaling_mode = single-job
     resources_available.scratch_iops = 0
     resources_available.scratch_size = 0gb
     resources_available.stack_id = arn:aws:cloudformation:us-east-2:<REDACTED>:stack/idea-demo-compute-ondemand-103/9caa5e10-7ba3-11ed-837d-060b0d4719a4
     resources_available.subnet_id = subnet-064c905368b057b68
     resources_available.system_metrics = False
     resources_available.tenancy = default
     resources_available.terminate_when_idle = 0
     resources_available.vnode = ip-10-110-6-141
     resources_assigned.accelerator_memory = 0kb
     resources_assigned.hbmem = 0kb
     resources_assigned.mem = 0kb
     resources_assigned.naccelerators = 0
     resources_assigned.ncpus = 0
     resources_assigned.vmem = 0kb
     queue = normal
     resv_enable = True
     sharing = default_shared
     license = l
     last_state_change_time = Wed Dec 14 11:49:10 2022
```

## 4 - Restart the Scheduler&#x20;

If needed, SSH to the scheduler machine and restart both OpenPBS and  `idea-scheduler` module.

To restart OpenPBS, run `systemctl restart pbs`

A valid output looks like this ( see `Active: active (running)`)

```
[clusteradmin@ip-10-110-2-204 ~]$ systemctl status pbs
● pbs.service - Portable Batch System
   Loaded: loaded (/opt/pbs/libexec/pbs_init.d; enabled; vendor preset: disabled)
   Active: active (running) since Wed 2022-12-14 11:22:30 UTC; 2min 13s ago
     Docs: man:pbs(8)
  Process: 17146 ExecStop=/opt/pbs/libexec/pbs_init.d stop (code=exited, status=0/SUCCESS)
  Process: 20115 ExecStart=/opt/pbs/libexec/pbs_init.d start (code=exited, status=0/SUCCESS)
    Tasks: 9
   Memory: 17.0M
   CGroup: /system.slice/pbs.service
           ├─20191 /opt/pbs/sbin/pbs_comm
           ├─20206 /opt/pbs/sbin/pbs_sched
           ├─20249 /opt/pbs/sbin/pbs_ds_monitor monitor
           ├─20273 /usr/bin/postgres -D /var/spool/pbs/datastore -p 15007
           ├─20283 postgres: logger process
           ├─20285 postgres: checkpointer process
           ├─20286 postgres: writer process
           ├─20287 postgres: wal writer process
           ├─20288 postgres: autovacuum launcher process
           ├─20289 postgres: stats collector process
           ├─20318 postgres: postgres pbs_datastore 10.110.2.204(60770) idle
           └─20321 /opt/pbs/sbin/pbs_server.bin
```

If the service is not starting up correctly, verify the logs under:

* &#x20;`/var/spool/pbs/sched_logs`&#x20;
* &#x20;`/var/spool/pbs/server_logs`



Finally, try to restart `idea-scheduler` by running  `/opt/idea/python/latest/bin/supervisorctl restart scheduler`

You can confirm if `idea-scheduler` has started correctly by checking the application logs located under `/opt/idea/app/logs`
