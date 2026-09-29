# API tokens

Open **My account**, then **API tokens**. Choose **Create token**, enter a name,
and select an expiry of 30, 90, 180, or 365 days. Copy the token from the confirmation
modal before closing it: it will not be shown again. Store it securely for your automation.

Send it as a bearer credential to the service API endpoint. For example, with the
cluster-manager API URL in `IDEA_API_URL` and your token in `IDEA_API_TOKEN`:

```bash
curl "$IDEA_API_URL" \
  -H "Authorization: Bearer $IDEA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"header":{"namespace":"Auth.GetUser"},"payload":{}}'
```

Tokens also work with the scheduler and virtual desktop controller API endpoints.
They act as your user, with your current groups and permissions checked on every
call. An administrator token has administrator rights. Group removal removes that
access on the next call, and disabled or deleted accounts cannot use tokens.
Tokens cannot grant permissions beyond those of their owner.

Choose **Revoke** beside a token to delete it. Revocation takes effect across services
within 60 seconds. Expired tokens are refused even while cached. Create a replacement
before expiry when automation must continue. Only a SHA-256 hash is stored; a lost
token must be replaced. Last used is recorded at most once per minute.

The Auth API offers `CreateApiToken` (`name`, `expires_in_days`, from 1 to 365),
`ListApiTokens`, and `DeleteApiToken` (`token_id`). Creation always uses the caller
as owner. Users list and revoke their own tokens; administrators can also list another
user with `ListApiTokens {"username":"user-a"}` and revoke another user's token by id.
