import os
import unittest
from unittest.mock import patch

import api as grid_api
import settings as grid_settings
import httpx

class StatusAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_requires_valid_admin_cookie(self):
        with patch.dict(os.environ, {
            'BOT_ADMIN_PASSWORD': 'unique-private-admin-password',
            'BOT_JWT_SECRET': 'a-random-private-signing-secret-32-chars',
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=grid_api.app),
                base_url='https://testserver',
            ) as client:
                for token in (None, 'invalid-token'):
                    cookies = {} if token is None else {grid_settings.AUTH_COOKIE_NAME: token}
                    client.cookies.clear()
                    client.cookies.update(cookies)
                    response = await client.get('/api/bot/status')
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json(), {'detail': 'Invalid or expired token.'})
                client.cookies.clear()
                login = await client.post('/api/auth/login',
                    json={'password': 'unique-private-admin-password'},
                    headers={'Origin': grid_settings.FRONTEND_ORIGIN})
                self.assertEqual(login.status_code, 200)
                # Authentication succeeds before the status implementation is reached.
                with patch.object(grid_api, 'GridDatabase', side_effect=RuntimeError('authenticated')):
                    with self.assertRaisesRegex(RuntimeError, 'authenticated'):
                        await client.get('/api/bot/status')

if __name__ == '__main__':
    unittest.main()
