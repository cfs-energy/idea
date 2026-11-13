// One flat character class. A repeated group around "+" takes exponential time on a name that
// fails near its end, and these run on every keystroke.
export const GROUP_OR_PROJECT_NAME_PATTERN = '^[a-z0-9-]{3,}$';
export const PROJECT_TITLE_PATTERN = '^[A-Za-z0-9- ]{3,}$';
export const EMAIL_TEMPLATE_NAME_PATTERN = '^[a-z0-9-._]{3,}$';
