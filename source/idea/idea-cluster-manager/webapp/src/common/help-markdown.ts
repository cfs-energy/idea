import type {Options} from 'react-markdown';
import rehypeRaw from 'rehype-raw';

// The bundled help pages are Markdown plus a little raw HTML (collapsible <details> sections).
// Only the elements and attributes Markdown and those pages produce are kept; any other element is
// removed with its content, and any other attribute is dropped.
const ALLOWED: Record<string, string[]> = {
    a: ['href', 'title'], ol: ['start'], code: ['className'], th: ['align'], td: ['align'],
    ...Object.fromEntries('b blockquote br del details em h1 h2 h3 h4 h5 h6 hr li p pre strong summary table tbody thead tr ul'.split(' ').map(tag => [tag, []])),
};

interface HastNode {type: string; tagName?: string; properties?: Record<string, unknown>; children?: HastNode[]}

function allowlist(node: HastNode) {
    if (!node.children) return;
    node.children = node.children.filter(child => child.type !== 'element' || Object.hasOwn(ALLOWED, child.tagName!));
    for (const child of node.children) {
        if (child.type === 'element') {
            child.properties = Object.fromEntries(Object.entries(child.properties ?? {}).filter(([name]) => ALLOWED[child.tagName!].includes(name)));
        }
        allowlist(child);
    }
}

export const HELP_REHYPE_PLUGINS: Options['rehypePlugins'] = [rehypeRaw, () => (tree: HastNode) => allowlist(tree)];
