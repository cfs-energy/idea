/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import { IdeaApiInvoker } from './idea-api-invoker';
import { initTestAppData } from '../test-support';

function buildInvoker(postMessage: (message: any, transfer: any[]) => void): IdeaApiInvoker {
    initTestAppData();
    return new IdeaApiInvoker({
        name: 'test-invoker',
        url: 'http://localhost:8080/api/v1',
        serviceWorkerRegistration: {
            active: {
                postMessage: postMessage
            }
        } as any
    });
}

describe('idea api invoker', () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    // the network timeout lives inside the service worker, so a worker that never replies used
    // to leave the caller pending forever - the request looks like it is still in flight.
    it('fails the request when the service worker never replies', async () => {
        vi.useFakeTimers();
        const invoker = buildInvoker(() => {});

        const request = invoker.invoke_alt('Scheduler.SubmitJob', { dry_run: false });
        const settled = request.then(
            () => 'resolved',
            (error) => error
        );

        await vi.advanceTimersByTimeAsync(600000);
        expect(await Promise.race([settled, Promise.resolve('pending')])).toBe('pending');

        await vi.advanceTimersByTimeAsync(60001);
        const error: any = await settled;
        expect(error.errorCode).toBe('REQUEST_TIMEOUT');
        expect(error.message).toBe('Request timed-out');
    });

    it('returns the payload when the service worker replies', async () => {
        const invoker = buildInvoker((message, transfer) => {
            transfer[0].postMessage({
                response: { success: true, payload: { job: { job_id: '101' } } }
            });
        });

        const result: any = await invoker.invoke_alt('Scheduler.SubmitJob', { dry_run: false });
        expect(result.job.job_id).toBe('101');
    });
});
