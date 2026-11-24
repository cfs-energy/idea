// the browser reads the blob after click() returns, so the url is released a little later
const REVOKE_AFTER_MS = 30_000;

export function downloadCsv(filename: string, content: string, type = 'text/csv;charset=utf-8') {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const link = document.createElement('a');
    try {link.href = url; link.download = filename; document.body.appendChild(link); link.click();}
    finally {link.remove(); setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);}
}

// A text cell that a spreadsheet would read as a formula gets a leading quote. A lone "-" placeholder
// and a plain signed number are left as they are, so they still read as a dash and as a number.
const LITERAL = /^(-|[+-]?\d+(\.\d+)?)$/;
export const csvCell = (value: unknown) => `"${(typeof value === 'string' && !LITERAL.test(value) ? value.replace(/^(\s*[=+@-])/, "'$1") : String(value ?? '')).replaceAll('"', '""')}"`;
