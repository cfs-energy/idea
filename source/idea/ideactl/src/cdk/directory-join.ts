/** Host directory setup runs after the ECS agent can receive the automation task. */
export function directoryJoinCommands(input: {
  packageUri: string;
  clusterName: string;
  ecsClusterName: string;
  region: string;
  provider: string;
  domain?: string;
}): string[] {
  const quote = (value: string): string => `'${value.replace(/'/g, `'"'"'`)}'`;
  const membership = input.provider === "openldap"
    ? "openssl x509 -noout -in /etc/openldap/cacerts/openldap-server.pem"
    : `adcli testjoin --domain=${quote(input.domain ?? "")}`;
  return [
    // realmd refuses to join unless every file it lists exists, including oddjob-mkhomedir's helper.
    "dnf install -y sssd sssd-ad sssd-ldap sssd-tools adcli realmd krb5-workstation authselect oddjob oddjob-mkhomedir jq awscli",
    "dnf install -y nfs-utils openldap-clients openssl which",
    "install -d -m 0700 /var/lib/idea/directory",
    "cat > /usr/local/sbin/idea-directory-join <<'IDEA_DIRECTORY_WRAPPER'",
    "#!/bin/bash",
    "set -o pipefail",
    `export AWS_REGION=${quote(input.region)} AWS_DEFAULT_REGION=${quote(input.region)} IDEA_CLUSTER_NAME=${quote(input.clusterName)}`,
    "export AWS_PAGER=''",
    // The shared AL2023 template still uses authconfig. These hosts use authselect.
    "authconfig() {",
    '  if [[ " $* " == *" --enablesssd "* ]]; then authselect select sssd --force; fi',
    "}",
    "export -f authconfig",
    // Not `sssctl config-check`: it rejects the [secrets] section the shared template still writes.
    "directory_ready() {",
    `  test -s /etc/sssd/sssd.conf && ${membership} && systemctl is-active --quiet sssd`,
    "}",
    "join_directory() {",
    "  if [[ -f /var/lib/idea/directory-joined ]]; then directory_ready; return $?; fi",
    // A host that already passes the membership test keeps its join; the template would leave and rejoin.
    `  if ! ${membership}; then`,
    `    aws s3 cp ${quote(input.packageUri)} /var/lib/idea/directory/package.tar.gz || return 1`,
    "    tar -xzf /var/lib/idea/directory/package.tar.gz -C /var/lib/idea/directory || return 1",
    "    mkdir -p /etc/openldap/cacerts || return 1",
    "    bash /var/lib/idea/directory/ecs-host/directory_join.sh || return 1",
    "  fi",
    "  authselect select sssd --force || return 1",
    ...(input.provider === "openldap" ? ["  openssl rehash /etc/openldap/cacerts || return 1"] : []),
    "  systemctl enable sssd && systemctl restart sssd || return 1",
    "  directory_ready || return 1",
    "  touch /var/lib/idea/directory-joined",
    "}",
    "while (( SECONDS < 7200 )); do",
    "  if join_directory && nfsidmap -c; then",
    "    container_instance=$(curl -fsS --max-time 10 http://localhost:51678/v1/metadata | jq -er '.ContainerInstanceArn | select(type == \"string\" and length > 0)')",
    `    if [[ -n "$container_instance" ]] && aws ecs put-attributes --cluster ${quote(input.ecsClusterName)} --attributes "name=idea.directory,value=joined,targetId=$container_instance"; then`,
    '      echo "Directory joined; ECS placement attribute published"',
    "      exit 0",
    "    fi",
    "  fi",
    '  echo "Directory join or placement attribute failed; retrying in 30 seconds"',
    "  sleep 30",
    "done",
    'echo "Directory join timed out" >&2',
    "exit 1",
    "IDEA_DIRECTORY_WRAPPER",
    "chmod 0700 /usr/local/sbin/idea-directory-join",
    "cat > /etc/systemd/system/idea-directory-join.service <<'IDEA_DIRECTORY_UNIT'",
    "[Unit]",
    "Description=Join the host directory and publish ECS placement readiness",
    "Wants=network-online.target ecs.service",
    "After=ecs.service network-online.target",
    "[Service]",
    "Type=oneshot",
    "RemainAfterExit=yes",
    "TimeoutStartSec=2h",
    "ExecStart=/usr/local/sbin/idea-directory-join",
    "StandardOutput=journal",
    "StandardError=journal",
    "[Install]",
    "WantedBy=multi-user.target",
    "IDEA_DIRECTORY_UNIT",
    "systemctl daemon-reload",
    "systemctl enable idea-directory-join.service",
    "systemctl start --no-block idea-directory-join.service",
  ];
}
