import React from 'react';
import { render } from '@testing-library/react';
import { ColumnLayout } from '@cloudscape-design/components';
import { fingerprint } from './dom-fingerprint';

function html(markup: string): Element {
    const host = document.createElement('div');
    host.innerHTML = markup;
    return host;
}

// The same side-navigation classes as shipped by two Cloudscape releases, plus a release that renamed
// one of them. Real hashes, so the fixtures carry the shape the normalizer has to cope with.
// awsui-hashed-class-name-allowed: normalization fixture
const RELEASE_A = 'awsui_root_l0dv0_1mtlo_93 awsui_link_l0dv0_1mtlo_180';
// awsui-hashed-class-name-allowed: normalization fixture
const RELEASE_B = 'awsui_root_l0dv0_s3klw_99 awsui_link_l0dv0_s3klw_253';
// awsui-hashed-class-name-allowed: normalization fixture
const RELEASE_B_RENAMED = 'awsui_container_l0dv0_s3klw_99 awsui_link_l0dv0_s3klw_253';

// Chart and code-editor internals use BEM element names, whose <name> segment
// contains an underscore of its own. Real names from the installed package.
// awsui-hashed-class-name-allowed: normalization fixture
const BEM_RELEASE_A = 'awsui_segment__path_1edmh_ce51y_161 awsui_ticks__text_f0fot_1e124_175';
// awsui-hashed-class-name-allowed: normalization fixture
const BEM_RELEASE_B = 'awsui_segment__path_1edmh_p4k2v_204 awsui_ticks__text_f0fot_p4k2v_233';

describe('dom fingerprint normalization', () => {
    it('absorbs the per-release hash in Cloudscape class names', () => {
        expect(fingerprint(html(`<p class="${RELEASE_A}"></p>`))).toBe(fingerprint(html(`<p class="${RELEASE_B}"></p>`)));
        expect(fingerprint(html(`<p class="${RELEASE_A}"></p>`))).toContain('awsui_link awsui_root');
    });

    it('absorbs the per-release hash in BEM-style Cloudscape class names', () => {
        expect(fingerprint(html(`<p class="${BEM_RELEASE_A}"></p>`))).toBe(fingerprint(html(`<p class="${BEM_RELEASE_B}"></p>`)));
        expect(fingerprint(html(`<p class="${BEM_RELEASE_A}"></p>`))).toContain('awsui_segment__path awsui_ticks__text');
    });

    it('does not absorb a change to the Cloudscape class name itself', () => {
        expect(fingerprint(html(`<p class="${RELEASE_B}"></p>`))).not.toBe(fingerprint(html(`<p class="${RELEASE_B_RENAMED}"></p>`)));
    });

    it('absorbs React useId values while keeping the reference they encode', () => {
        const react19 = html('<label for="formField_r_2h_"></label><input id="formField_r_2h_">');
        const react18 = html('<label for="formField:r9:"></label><input id="formField:r9:">');
        expect(fingerprint(react19)).toBe(fingerprint(react18));

        const broken = html('<label for="formField_r_2h_"></label><input id="formField_r_5b_">');
        expect(fingerprint(broken)).not.toBe(fingerprint(react19));
    });

    it('reports a structural change', () => {
        const before = html('<div><span></span></div>');
        const after = html('<div><span></span><span></span></div>');
        expect(fingerprint(before)).not.toBe(fingerprint(after));
    });

    it('reports an icon geometry change through the path digest', () => {
        const before = html('<svg><path d="M1 1h4"></path></svg>');
        const after = html('<svg><path d="M2 2h8"></path></svg>');
        expect(fingerprint(before)).not.toBe(fingerprint(after));
        expect(fingerprint(before)).toMatch(/d="#[0-9a-f]{8}"/);
    });

    // The regression the React 19 probe found: Cloudscape stopped flattening React.Fragment children of
    // ColumnLayout, so a fragment collapses into one grid cell. The fingerprint sees it; text does not.
    it('distinguishes fragment children of ColumnLayout from an array', () => {
        const fragment = render(
            <ColumnLayout columns={3}>
                {/* cloudscape-fragment-child-allowed: this is the regression fixture */}
                <React.Fragment key="a">
                    <div>one</div>
                    <div>two</div>
                    <div>three</div>
                </React.Fragment>
            </ColumnLayout>
        );
        const asFragment = fingerprint(fragment.container);
        fragment.unmount();

        const array = render(
            <ColumnLayout columns={3}>
                {[<div key="1">one</div>, <div key="2">two</div>, <div key="3">three</div>]}
            </ColumnLayout>
        );
        const asArray = fingerprint(array.container);
        array.unmount();

        expect(asFragment).not.toBe(asArray);
    });
});
