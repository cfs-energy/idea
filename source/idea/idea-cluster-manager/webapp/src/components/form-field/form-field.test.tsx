import {createRef} from 'react';
import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {describe, expect, it, vi} from 'vitest';
import SampleJobParams from '../../pages/hpc/sample-job-params.json';
import IdeaForm from '../form';
import {LEGACY_PATTERNS} from './form-field';
import {initTestAppContext} from '../../test-support';

it('refuses a new password whose two fields stopped matching after they matched once', async () => {
    initTestAppContext();
    const form = createRef<IdeaForm>();
    render(<IdeaForm ref={form} name="marker-form" showActions={false}
                     params={[{name: 'password', title: 'Password', description: 'enter a password', data_type: 'str', param_type: 'new-password', validate: {required: true}}]}/>);
    const [first, second] = document.querySelectorAll('input[type="password"]') as NodeListOf<HTMLInputElement>;
    const user = userEvent.setup();
    await user.type(first, 'MarkerOne1!');
    await user.type(second, 'MarkerOne1!');
    expect(form.current!.validate()).toBe(true);
    await user.clear(first);
    await user.type(first, 'MarkerTwo2!');
    expect(form.current!.validate()).toBe(false);
    expect((await screen.findAllByText('Passwords do not match')).length).toBeGreaterThan(0);
});

describe('the job name rule', () => {
    const fieldWith = (regex: string) => {
        initTestAppContext();
        const form = createRef<IdeaForm>();
        render(<IdeaForm ref={form} name="marker-form" showActions={false}
                         params={[{name: 'job_name', title: 'Job Name', data_type: 'str', param_type: 'text', validate: {required: true, regex}}]}/>);
        return form;
    };
    const definition = (SampleJobParams as any[]).find(param => param.name === 'job_name').validate.regex;

    it('is the same rule in the stock form and in the check for stored older forms', () => {
        expect(definition).toBe('^[A-Za-z0-9_-]{3,50}$');
        expect(LEGACY_PATTERNS['^([A-Za-z0-9_-]+){3,50}$']).toBe(definition);
    });

    it.each([[definition], ['^([A-Za-z0-9_-]+){3,50}$']])('%s accepts 3 to 50 characters and answers at once', async regex => {
        const form = fieldWith(regex);
        await vi.waitFor(() => expect(form.current!.getFormField('job_name')).toBeTruthy());
        const field = form.current!.getFormField('job_name')!;
        expect(['ab', 'abc', 'a'.repeat(50), 'a'.repeat(51)].map(name => field.validateRegex(name))).toEqual([false, true, true, false]);
        const start = performance.now();
        expect(field.validateRegex(`${'a'.repeat(40)}!`)).toBe(false);
        expect(performance.now() - start).toBeLessThan(50);
    });
});

it('keeps the fraction of a float field', async () => {
    initTestAppContext();
    const form = createRef<IdeaForm>();
    render(<IdeaForm ref={form} name="marker-form" showActions={false}
                     params={[{name: 'ratio', title: 'Ratio', data_type: 'float', param_type: 'text', default: '2.5'},
                              {name: 'count', title: 'Count', data_type: 'int', param_type: 'text', default: '2.5'}]}/>);
    await vi.waitFor(() => expect(form.current!.getValues().ratio).toBe(2.5));
    expect(form.current!.getValues().count).toBe(2);
});
