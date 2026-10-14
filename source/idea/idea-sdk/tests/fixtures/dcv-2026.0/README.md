Vendor systemd units extracted on 2026-10-14 from DCV 2026.0.24952-1.
Both files are byte-identical in all four server packages:

- `https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-el10-x86_64.tgz`
- `https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-el10-aarch64.tgz`
- `https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-ubuntu2604-x86_64.tgz`
- `https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-ubuntu2604-aarch64.tgz`

SHA-256:

```text
29ac7fcf8bc5dc7df96769d9dfa9edf60bb77624fecd5794848b27065dbf54b2  dcvserver.service
afc82362da0ff0a8c43080b20cf25ac0661aaff7ce2c38e78aa884af4502642f  dcvsessionlauncher.service
```

`dcvserver.service` creates `/run/dcv` and `/run/dcvlogon` through
`RuntimeDirectory=dcv dcvlogon`, owned by `dcv:dcv` with systemd's default mode
0755. The launcher uses `BindsTo=dcvserver.service` without `After=` and needs
`/run/dcv` to exist before setting up its mount namespace. The desktop bootstrap
used to overwrite the server unit, removing its runtime directories and sandbox.
