# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
import logging

from odoo import http
from odoo.addons.web.controllers.session import Session
from odoo.http import request

_logger = logging.getLogger(__name__)


class EndSessionLogout(Session):
    @http.route()
    def logout(self, redirect="/odoo"):
        # Read before the Odoo session is gone: afterwards there is no uid.
        target = self._provider_logout_url()
        response = super().logout(redirect=redirect)
        if not target:
            return response
        return request.redirect(target, 303, local=False)

    def _provider_logout_url(self):
        uid = request.session.uid
        if not uid or not request.db:
            return None
        try:
            user = request.env["res.users"].sudo().browse(uid).exists()
            provider = user.oauth_provider_id
            if not provider:
                return None
            return provider._end_session_url(request.httprequest.url_root)
        except Exception:
            # Logging out must always work. Failing to build the provider URL
            # only means the provider session survives, as without this addon.
            _logger.exception("could not build the end-session URL for uid %s", uid)
            return None
