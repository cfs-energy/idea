import nunjucks from 'nunjucks'
import {buildPbsSelect, isPbsSelectLine, mergePbsSelect, parsePbsSelect, updateJobScriptSelect} from './pbs-select'

describe('isPbsSelectLine', () => {
    it('matches a select directive', () => {
        expect(isPbsSelectLine('#PBS -l select=4:ncpus=24')).toBe(true)
        expect(isPbsSelectLine('#PBS  -l  select=4')).toBe(true)
    })
    it('does not match other directives or comments', () => {
        expect(isPbsSelectLine('#PBS -l walltime=01:00:00')).toBe(false)
        expect(isPbsSelectLine('## #PBS -l select=4:ncpus=24')).toBe(false)
        expect(isPbsSelectLine('echo select=4')).toBe(false)
    })
})

describe('parsePbsSelect', () => {
    it('parses count and chunk resources', () => {
        const select = parsePbsSelect('#PBS -l select=4:ncpus=24:mpiprocs=24:place=scatter')
        expect(select).not.toBeNull()
        expect(select!.count).toBe(4)
        expect(select!.resources).toEqual([
            {key: 'ncpus', value: '24'},
            {key: 'mpiprocs', value: '24'},
            {key: 'place', value: 'scatter'}
        ])
    })
    it('defaults the count to 1 when omitted', () => {
        const select = parsePbsSelect('#PBS -l select=ncpus=24')
        expect(select!.count).toBe(1)
        expect(select!.resources).toEqual([{key: 'ncpus', value: '24'}])
    })
    it('returns null for a chunk list', () => {
        expect(parsePbsSelect('#PBS -l select=2:ncpus=24+1:ncpus=8')).toBeNull()
    })
    it('returns null for a non-select line', () => {
        expect(parsePbsSelect('#PBS -l walltime=01:00:00')).toBeNull()
    })
})

describe('mergePbsSelect', () => {
    it('preserves chunk resources the portal does not own', () => {
        expect(mergePbsSelect('#PBS -l select=4:ncpus=24:mpiprocs=24:place=scatter:mem=100gb', 2, 96))
            .toBe('#PBS -l select=2:ncpus=96:mpiprocs=96:place=scatter:mem=100gb')
    })
    it('leaves mpiprocs alone when it was not one rank per cpu', () => {
        expect(mergePbsSelect('#PBS -l select=4:ncpus=24:mpiprocs=2', 2, 96))
            .toBe('#PBS -l select=2:ncpus=96:mpiprocs=2')
    })
    it('does not add mpiprocs when the template had none', () => {
        expect(mergePbsSelect('#PBS -l select=4:ncpus=24', 2, 96)).toBe('#PBS -l select=2:ncpus=96')
    })
    it('adds ncpus when the template had none', () => {
        expect(mergePbsSelect('#PBS -l select=4:mem=100gb', 2, 96)).toBe('#PBS -l select=2:mem=100gb:ncpus=96')
    })
    it('preserves other resources on the same line', () => {
        expect(mergePbsSelect('#PBS -l select=4:ncpus=24 -l place=scatter', 2, 96))
            .toBe('#PBS -l select=2:ncpus=96 -l place=scatter')
    })
    it('leaves a chunk list untouched', () => {
        const line = '#PBS -l select=2:ncpus=24+1:ncpus=8'
        expect(mergePbsSelect(line, 4, 96)).toBe(line)
    })
    it('leaves a non-select line untouched', () => {
        const line = '#PBS -l walltime=01:00:00'
        expect(mergePbsSelect(line, 4, 96)).toBe(line)
    })
    it('sizes an empty select value', () => {
        expect(mergePbsSelect('#PBS -l select=', 2, 96)).toBe('#PBS -l select=2:ncpus=96')
    })
    it('merges a CRLF line and preserves the trailing carriage return', () => {
        expect(mergePbsSelect('#PBS -l select=2:ncpus=24:mpiprocs=24\r', 8, 96))
            .toBe('#PBS -l select=8:ncpus=96:mpiprocs=96\r')
    })
    it('leaves an unrendered template expression untouched', () => {
        // updateJobScriptSelect runs before nunjucks renders the template; rewriting
        // `{{ ncpus }}` would truncate the value and orphan the closing braces
        const spaced = '#PBS -l select=1:ncpus={{ ncpus }}'
        expect(mergePbsSelect(spaced, 8, 96)).toBe(spaced)
        const unspaced = '#PBS -l select=1:ncpus={{ncpus}}'
        expect(mergePbsSelect(unspaced, 8, 96)).toBe(unspaced)
    })
})

describe('buildPbsSelect', () => {
    it('emits count and ncpus only', () => {
        expect(buildPbsSelect(3, 48)).toBe('#PBS -l select=3:ncpus=48')
    })
})

describe('updateJobScriptSelect', () => {
    const jobScript = [
        '#!/bin/bash',
        '#PBS -q comsol',
        '#PBS -l select=4:ncpus=24:mpiprocs=24 -l place=scatter',
        '#PBS -l walltime=01:00:00',
        '',
        'cd "$PBS_O_WORKDIR"'
    ].join('\n')

    it('updates the select directive in place and leaves the rest of the script alone', () => {
        expect(updateJobScriptSelect(jobScript, 2, 96)).toBe([
            '#!/bin/bash',
            '#PBS -q comsol',
            '#PBS -l select=2:ncpus=96:mpiprocs=96 -l place=scatter',
            '#PBS -l walltime=01:00:00',
            '',
            'cd "$PBS_O_WORKDIR"'
        ].join('\n'))
    })

    it('adds a select directive below the shebang when the template has none', () => {
        expect(updateJobScriptSelect('#!/bin/bash\n#PBS -q comsol', 2, 96)).toBe([
            '#!/bin/bash',
            '#PBS -l select=2:ncpus=96',
            '# Added by IDEA Web Portal',
            '#PBS -q comsol'
        ].join('\n'))
    })

    it('adds a select directive at the top when there is no shebang', () => {
        expect(updateJobScriptSelect('#PBS -q comsol', 2, 96)).toBe([
            '#PBS -l select=2:ncpus=96',
            '# Added by IDEA Web Portal',
            '#PBS -q comsol'
        ].join('\n'))
    })

    it('updates every select directive in the script', () => {
        const script = '#PBS -l select=1:ncpus=2\n#PBS -l select=1:ncpus=2'
        expect(updateJobScriptSelect(script, 3, 8)).toBe('#PBS -l select=3:ncpus=8\n#PBS -l select=3:ncpus=8')
    })

    it('updates a CRLF script in place without inserting a second directive', () => {
        const script = '#!/bin/bash\r\n#PBS -l select=2:ncpus=24:mpiprocs=24\r\necho hi\r\n'
        expect(updateJobScriptSelect(script, 8, 96))
            .toBe('#!/bin/bash\r\n#PBS -l select=8:ncpus=96:mpiprocs=96\r\necho hi\r\n')
    })
})

describe('templated select directives', () => {
    // a select line is only a parsable PBS directive once the placeholders are gone, which is why
    // the submit page renders the template before it applies the node count and ncpus.
    const jinja2Template = [
        '#!/bin/bash',
        '#PBS -l select={{ nodes }}:ncpus={{ ncpus }}:mpiprocs={{ ncpus }} -l place=scatter',
        ''
    ].join('\n')

    it('leaves a jinja2 select line untouched while it still carries placeholders', () => {
        expect(updateJobScriptSelect(jinja2Template, 2, 96)).toBe(jinja2Template)
        expect(updateJobScriptSelect(jinja2Template, 2, 96)).not.toContain('# Added by IDEA Web Portal')
    })

    it('applies the node count and ncpus to the rendered jinja2 select line', () => {
        const rendered = nunjucks.renderString(jinja2Template, {nodes: 4, ncpus: 24})
        expect(rendered).toContain('#PBS -l select=4:ncpus=24:mpiprocs=24 -l place=scatter')
        expect(updateJobScriptSelect(rendered, 2, 96)).toBe([
            '#!/bin/bash',
            '#PBS -l select=2:ncpus=96:mpiprocs=96 -l place=scatter',
            ''
        ].join('\n'))
    })

    it('leaves a percent placeholder select line untouched until it is substituted', () => {
        const template = '#PBS -l select=%nodes%:ncpus=%ncpus%:mem=32gb'
        expect(updateJobScriptSelect(template, 2, 96)).toBe(template)
        const substituted = template.replaceAll('%nodes%', '4').replaceAll('%ncpus%', '24')
        expect(updateJobScriptSelect(substituted, 2, 96)).toBe('#PBS -l select=2:ncpus=96:mem=32gb')
    })
})
