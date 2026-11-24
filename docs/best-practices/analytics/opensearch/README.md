# OpenSearch

## Network access to the domain

On a domain the cluster creates, only the portal, the scheduler, the desktop controller and the analytics sink reach it on port 443, each by a rule in its own stack. On an imported domain (`analytics.opensearch.use_existing`) those rules are added to a group the domain does not use, so who can reach the domain is whatever you set on it yourself.
