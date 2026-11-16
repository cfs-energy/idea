import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

// vitest runs from the webapp directory; import.meta.url is not a file URL under jsdom.
const SCRIPT = resolve('build-support/lint-cloudscape-usage.mjs')

// Reported lines are the ones the TypeScript-compiler version of the script gave for the same files.
function lint(files: Record<string, string>): { status: number | null; findings: string[]; stderr: string } {
    const directory = mkdtempSync(join(tmpdir(), 'lint-cloudscape-'))
    try {
        const paths = Object.entries(files).map(([name, text]) => {
            const path = join(directory, name)
            writeFileSync(path, text)
            return path
        })
        const result = spawnSync(process.execPath, [SCRIPT, ...paths], { encoding: 'utf8' })
        const findings = result.stderr
            .split('\n')
            .filter((line) => line.startsWith('  '))
            .map((line) => line.slice(line.lastIndexOf('/') + 1))
        return { status: result.status, findings, stderr: result.stderr }
    } finally {
        rmSync(directory, { recursive: true, force: true })
    }
}

describe('lint-cloudscape-usage', () => {
    it('passes clean files', () => {
        expect(lint({ 'Clean.tsx': 'export const A = () => <SpaceBetween>{[<span key="a" />].length}<div>x</div></SpaceBetween>\n' }).status).toBe(0)
    })

    it('reports each rule on the line the TypeScript scanner gives', () => {
        const { status, findings } = lint({
            'Broken.tsx': [
                "const hashed = 'awsui_button_vjswe_1x2y3_101'",
                '// awsui-hashed-class-name-allowed: fixture',
                "const allowed = 'awsui_trigger-button_dwuol_abc12_42'",
                'export const A = (p: { show: boolean; items: string[] }) => (',
                '    <SpaceBetween size="s">',
                '        <>one</>',
                '        <React.Fragment>two</React.Fragment>',
                '        <React.Fragment />',
                '        {/* cloudscape-fragment-child-allowed: test */}',
                '        <>allowed</>',
                '        {p.show ? <>yes</> : <Fragment>no</Fragment>}',
                '        {p.items.map((i) => <div key={i}><>inside an element</></div>)}',
                '        {...[<>spread</>]}',
                '        <Grid><ColumnLayout><Fragment>col</Fragment></ColumnLayout></Grid>',
                '    </SpaceBetween>',
                ')',
                'export const B = () => (',
                '    <div>',
                '        <b>bold</b><br />',
                '',
                '           &ensp;leading whitespace',
                '        {/* cloudscape-raw-markup-allowed: legacy */}',
                '        <i>allowed</i>',
                '        <TextContent><b>prose</b><p>a&nbsp;b</p></TextContent>',
                '        <Box icon={<small>attr</small>} />',
                '    </div>',
                ')',
                '',
            ].join('\n'),
            'Thing.test.tsx': 'export const T = () => (<SpaceBetween><>t</><b>t</b></SpaceBetween>)\n',
            'Crlf.tsx': 'export const C = () => (\r\n  <SpaceBetween>\r\n    <>crlf</>\r\n  </SpaceBetween>\r\n)\r\n',
            'Lsep.tsx': 'export const S = () => (\n  <div> <b>after a line separator</b></div>\n)\n',
            'style.scss': '.x { color: red; }\n.awsui_content_1a2b3_4c5d6_77 { margin: 0; }\n',
        })
        expect(status).toBe(1)
        expect(findings).toEqual([
            'Broken.tsx:1: awsui_button_vjswe_1x2y3_101',
            'style.scss:2: awsui_content_1a2b3_4c5d6_77',
            'Broken.tsx:6: fragment child of <SpaceBetween>',
            'Broken.tsx:7: fragment child of <SpaceBetween>',
            'Broken.tsx:11: fragment child of <SpaceBetween>',
            'Broken.tsx:11: fragment child of <SpaceBetween>',
            'Broken.tsx:13: fragment child of <SpaceBetween>',
            'Broken.tsx:14: fragment child of <ColumnLayout>',
            'Thing.test.tsx:1: fragment child of <SpaceBetween>',
            'Crlf.tsx:3: fragment child of <SpaceBetween>',
            'Broken.tsx:19: <b>',
            'Broken.tsx:19: <br>',
            'Broken.tsx:21: spacer entity',
            'Broken.tsx:24: spacer entity',
            'Broken.tsx:25: <small>',
            'Lsep.tsx:3: <b>',
        ])
    })

    it('fails a file it cannot parse instead of passing it unchecked', () => {
        const { status, stderr } = lint({ 'Bad.tsx': 'export const A = () => <b>x</b;\n' })
        expect(status).toBe(1)
        expect(stderr).toContain('Files the parser could not read')
        expect(stderr).toMatch(/Bad\.tsx:1: /)
    })
})
