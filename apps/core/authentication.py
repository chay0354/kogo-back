from rest_framework.authentication import TokenAuthentication


class CookieTokenAuthentication(TokenAuthentication):
    """
    Token auth that supports reading the DRF token from:
    - Authorization: Token <key>
    - Cookie: auth_token=<key>
    """

    cookie_name = 'auth_token'

    def authenticate(self, request):
        # 1) Standard DRF header token
        header_auth = super().authenticate(request)
        if header_auth:
            return header_auth

        # 2) Cookie token
        token = request.COOKIES.get(self.cookie_name)
        if not token:
            return None

        return self.authenticate_credentials(token)


