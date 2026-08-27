/** Read and update an OpenPBS `#PBS -l select=` directive in a job script template. The portal owns the
 * chunk count and ncpus; mpiprocs, ompthreads, mem and place are the application owner's and must survive
 * the update. Stricter than parse_select() in calculate_ncpus_hook.py, which rebuilds the statement. */

// captures: (1) the directive up to select=, (2) the select value, (3) anything that follows on the line.
// group 3 is [\s\S] not dot: `.` does not match `\r`, and a CRLF line must still parse (the trailing
// `\r` lands in the suffix and is preserved verbatim).
const SELECT_DIRECTIVE_REGEX = /^(\s*#PBS\s+-l\s+select=)(\S*)([\s\S]*)$/

export interface PbsChunkResource {
    key: string
    value: string
}

export interface PbsSelectStatement {
    // `#PBS -l select=` exactly as authored, including any leading whitespace
    prefix: string
    count: number
    resources: PbsChunkResource[]
    // anything following the select value on the same line, eg. ` -l place=scatter`
    suffix: string
}

export function isPbsSelectLine(line: string): boolean {
    return SELECT_DIRECTIVE_REGEX.test(line)
}

/** Parse a `#PBS -l select=` directive. Returns null when the line is not one, or when the select value
 * is not a single `[count:]key=value:...` chunk that can be safely rewritten. */
export function parsePbsSelect(line: string): PbsSelectStatement | null {
    const match = SELECT_DIRECTIVE_REGEX.exec(line)
    if (match === null) {
        return null
    }
    const prefix = match[1]
    const value = match[2]
    const suffix = match[3]
    if (value.length === 0) {
        return {prefix: prefix, count: 1, resources: [], suffix: suffix}
    }
    if (value.indexOf('+') >= 0) {
        // a chunk list (chunk+chunk) describes a heterogeneous layout that cannot be resized safely
        return null
    }

    const parts = value.split(':')
    let count = 1
    if (/^\d+$/.test(parts[0])) {
        count = parseInt(parts[0], 10)
        parts.shift()
    }

    const resources: PbsChunkResource[] = []
    for (let i = 0; i < parts.length; i++) {
        const separator = parts[i].indexOf('=')
        if (separator <= 0) {
            // not a key=value chunk resource: leave the directive to its author
            return null
        }
        const resourceValue = parts[i].substring(separator + 1)
        if (!/^[\w.+-]+$/.test(resourceValue)) {
            // not a plain token (eg. an unrendered `{{ ncpus }}` template expression split
            // at whitespace): rewriting would corrupt the directive - leave it alone
            return null
        }
        resources.push({
            key: parts[i].substring(0, separator),
            value: resourceValue
        })
    }
    return {prefix: prefix, count: count, resources: resources, suffix: suffix}
}

export function formatPbsSelect(select: PbsSelectStatement): string {
    let value = `${select.count}`
    select.resources.forEach((resource) => {
        value += `:${resource.key}=${resource.value}`
    })
    return `${select.prefix}${value}${select.suffix}`
}

export function getChunkResource(select: PbsSelectStatement, key: string): string | null {
    const found = select.resources.find((resource) => resource.key === key)
    if (!found) {
        return null
    }
    return found.value
}

export function setChunkResource(select: PbsSelectStatement, key: string, value: string) {
    const found = select.resources.find((resource) => resource.key === key)
    if (found) {
        found.value = value
    } else {
        select.resources.push({key: key, value: value})
    }
}

/** Update the chunk count and ncpus of an existing select directive, preserving every other chunk
 * resource and anything else on the line. Returns the line unchanged when it cannot be parsed. */
export function mergePbsSelect(line: string, count: number, ncpus: number): string {
    const select = parsePbsSelect(line)
    if (select === null) {
        return line
    }
    const previousNcpus = getChunkResource(select, 'ncpus')
    const previousMpiprocs = getChunkResource(select, 'mpiprocs')

    select.count = count
    setChunkResource(select, 'ncpus', `${ncpus}`)

    // a template asking for one MPI rank per cpu must keep doing so when the instance type changes ncpus
    if (previousMpiprocs !== null && previousMpiprocs === previousNcpus) {
        setChunkResource(select, 'mpiprocs', `${ncpus}`)
    }

    return formatPbsSelect(select)
}

/** Build a select directive for a template that does not have one. Chunk resources are not invented:
 * the scheduler applies the queue profile defaults. */
export function buildPbsSelect(count: number, ncpus: number): string {
    return `#PBS -l select=${count}:ncpus=${ncpus}`
}

/** Apply the chunk count and ncpus to a template: update the select directive if it has one,
 * otherwise add one below the shebang. */
export function updateJobScriptSelect(jobScript: string, count: number, ncpus: number): string {
    const lines = jobScript.split('\n')
    const updatedLines = []
    let shebangIndex = -1
    let selectUpdated = false
    for (let i = 0; i < lines.length; i++) {
        let line = lines[i]

        if (line.trim().startsWith('#!')) {
            shebangIndex = i
        } else if (isPbsSelectLine(line)) {
            line = mergePbsSelect(line, count, ncpus)
            selectUpdated = true
        }

        updatedLines.push(line)
    }

    if (!selectUpdated) {
        const select = buildPbsSelect(count, ncpus)
        if (shebangIndex >= 0) {
            updatedLines.splice(shebangIndex + 1, 0, select, '# Added by IDEA Web Portal')
        } else {
            updatedLines.splice(0, 0, select, '# Added by IDEA Web Portal')
        }
    }

    return updatedLines.join('\n')
}
