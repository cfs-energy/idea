import {readdirSync, readFileSync} from 'node:fs';
import {expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import {HELP_REHYPE_PLUGINS} from './help-markdown';

const help = (markdown: string) => renderToStaticMarkup(<ReactMarkdown children={markdown} remarkPlugins={[remarkGfm]} rehypePlugins={HELP_REHYPE_PLUGINS}/>);

it('renders every bundled help page as before, collapsible sections included', () => {
    const docs = readdirSync('src/docs').filter(name => name.endsWith('.md'));
    expect(docs.length).toBeGreaterThan(40);
    for (const name of docs) {
        const markdown = readFileSync(`src/docs/${name}`, 'utf8');
        const unfiltered = renderToStaticMarkup(<ReactMarkdown children={markdown} remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}/>);
        // the only attribute the pages carry that is dropped is a no-op hint on <summary>
        expect(help(markdown), name).toBe(unfiltered.replaceAll(' markdown="span"', ''));
    }
    expect(help(readFileSync('src/docs/my-virtual-desktop-sessions.md', 'utf8'))).toContain('<summary><b>Create a new desktop</b></summary>');
});

it('removes frames and scripts and drops attributes the help pages do not use', () => {
    const html = help('<iframe srcdoc="marker-frame"></iframe><script>marker-script</script>\n\n' +
        '<details ontoggle="marker-handler" style="color:red"><summary id="marker-id">Title</summary>\n\nkept text\n</details>');
    expect(html).not.toMatch(/iframe|srcdoc|marker-frame|script|marker-handler|style|marker-id/);
    expect(html).toContain('<details><summary>Title</summary>');
    expect(html).toContain('kept text');
});
