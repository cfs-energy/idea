/** The address to connect to the bastion host, the same wherever the portal shows one. */
export function bastionHost(moduleInfo: {ssh_hostname?: string, public?: unknown, public_ip?: string, private_ip?: string, private_dns_name?: string}): string {
    if (moduleInfo.ssh_hostname) {
        return moduleInfo.ssh_hostname
    }
    if (moduleInfo.public === true || moduleInfo.public === 'true') {
        return moduleInfo.public_ip ?? ''
    }
    return moduleInfo.private_ip || moduleInfo.private_dns_name || ''
}

export function describeKeyFailure(error: {message?: string}): string {
    return `Could not download your key: ${error?.message ?? 'unknown error'}. Try again, or contact your administrator.`
}
