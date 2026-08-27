import { IdeaAuthenticationContext } from './authentication-context';
import { initTestAppData } from '../test-support';

describe('IdeaAuthenticationContext', () => {
    beforeEach(() => {
        initTestAppData();
    });

    it('initializes in local-storage mode and reports logged out with no tokens', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.isLoggedIn()).resolves.toBe(false);
    });

    it('has no access token before authentication', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.getAccessToken()).rejects.toMatchObject({
            error_code: 'AUTH_TOKEN_EXPIRED'
        });
    });
});
