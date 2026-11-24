// One flat character class. A repeated group around "+" takes exponential time on a name that
// fails near its end, and these run on every keystroke.
export const GROUP_OR_PROJECT_NAME_PATTERN = '^[a-z0-9-]{3,}$';
export const PROJECT_TITLE_PATTERN = '^[A-Za-z0-9- ]{3,}$';
export const EMAIL_TEMPLATE_NAME_PATTERN = '^[a-z0-9-._]{3,}$';
// Same rule as the server (accounts/auth_constants.py USERNAME_REGEX).
// the server's rule without its lookbehind, which Safari before 16.4 cannot compile: the same names pass
export const USERNAME_PATTERN = '^(?=.{3,20}$)(?![_.-])(?!.*[_.-]{2})[a-z0-9_.-]*[a-z0-9]$';
export const USERNAME_RULE = 'Use 3 to 20 lowercase letters, digits, dots, underscores or hyphens. Start and end with a letter or digit, and never put two of . _ - in a row.';
// Same rule as the server (sessions/constants.py SESSION_NAME_PATTERN).
export const DESKTOP_NAME_PATTERN = '^[A-Za-z0-9 ._-]{1,64}$';
export const DESKTOP_NAME_RULE = 'Use 1 to 64 letters, digits, spaces, dots, underscores or hyphens.';
// The server only requires a name; one limit for create, clone, edit and create from a desktop.

/** The pattern, or the current value unchanged: the server checks only a changed name, so an older name can be kept. */
export function patternOrUnchanged(pattern: string, current?: string): string {
    return current ? `^(?:${current.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}|${pattern.slice(1, -1)})$` : pattern;
}
