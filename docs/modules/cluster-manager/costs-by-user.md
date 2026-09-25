# Costs by user

Open **Administration > Costs and usage > By user**. The table shows recorded AI, shared-storage, desktop, and job measurements. Select a user to open the five personal-cost facets, daily charts, and project AI use.

Values are estimates from stored records. **Not available**, **Partial**, and totals that exclude storage do not mean zero. See [My costs](../../first-time-users/my-costs.md) for the calculation and history rules.

## Enable shared-storage costs

EFS uses the existing measured-storage path. To collect ONTAP quota shares and estimate ONTAP costs:

1. Open **Administration > Settings > Costs > Storage cost collection**.
2. Enable storage collection. The metrics provider must be CloudWatch or DogStatsD.
3. For each ONTAP attachment, enter a read-only ONTAP account name.
4. Store its password in Secrets Manager and enter the secret ARN. Do not enter the password in Settings.
5. Save the settings, deploy any required secret or KMS permissions, and restart cluster-manager.
6. Wait for storage collection and the personal-cost refresh. Storage collection normally runs hourly. Personal-cost collection runs every 15 minutes and checks refresh requests each minute.

The account must reach the SVM HTTPS endpoint and read quota and volume data. Keep TLS verification enabled when the application image trusts the SVM certificate chain.

Storage cost is the daily storage rate estimate multiplied by the user's dated byte share. It does not require storage cost-allocation tags. ONTAP rates include provisioned SSD, throughput, IOPS above the included allowance, and capacity-pool bytes. EFS rates use storage class and exclude throughput and requests.

Users missing from a complete ONTAP quota report count as zero only when a default user quota rule exists. Historical days without a dated share remain missing. Complete quota shares and storage-tier footprints are retained so later billing revisions can be recalculated.
