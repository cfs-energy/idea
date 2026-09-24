import {ReactNode} from 'react';

export interface SettingsSection {id: string; label: string; content: ReactNode; keys?: string[]}
export interface SettingsSource {sections: SettingsSection[]; values: Record<string, any>; errors?: string[]}
export const EMPTY_SETTINGS: SettingsSource = {sections: [], values: {}, errors: []};
