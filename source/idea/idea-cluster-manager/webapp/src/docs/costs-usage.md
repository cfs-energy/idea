## Costs and usage

By user lists AI, shared storage, desktop and job measurements with Total sorted highest first. Selecting a user opens the five personal cost facets in a split panel, including daily charts, projects and their AI spend.

Storage is the user's apportioned shared-storage spend, and Storage GB is the measured shared usage behind that share. Values come from the same cached calculation as the personal cost billboard and may briefly show as unavailable while it refreshes.

To enable ONTAP storage costs, open **Settings > Costs > Storage cost collection**. Enable collection, set a read-only ONTAP username and password secret ARN for each attachment, save, deploy any required secret or KMS permissions, and restart cluster-manager. The provider must be CloudWatch or DogStatsD. Storage collection normally runs hourly. Personal-cost collection runs every 15 minutes and checks refresh requests each minute.

Storage uses daily rates and dated byte shares; it does not require storage cost-allocation tags. Historical days without a dated share remain unavailable. A user absent from a complete ONTAP quota report counts as zero only when a default user quota rule exists.
