# Copyright (C) 2020 GlodoUK <https://www.glodo.uk>
# Copyright (C) 2010-2016 XCG Consulting <http://odoo.consulting>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

import passlib

from odoo import SUPERUSER_ID, _, api, fields, models, tools
from odoo.exceptions import AccessDenied, ValidationError

_logger = logging.getLogger(__name__)


class ResUser(models.Model):
    """
    Add SAML login capabilities to Odoo users.
    """

    _inherit = "res.users"

    saml_ids = fields.One2many("res.users.saml", "user_id")

    @api.model
    def _saml_allowed_user_ids(self):
        """Users that can have a password even if the option to disallow it is set.
        It includes superuser and the admin if it exists.
        """
        allowed_users = {SUPERUSER_ID}
        user_admin = self.env.ref("base.user_admin", False)
        if user_admin:
            allowed_users.add(user_admin.id)
        return allowed_users

    def _set_password(self):
        """Inverse method of the password field."""
        # Redefine base method to block setting password on users with SAML ids
        # And also to be able to set password to a blank value
        if not self._allow_saml_and_password():
            saml_users = self.filtered(
                lambda user: user.sudo().saml_ids
                and self.id not in self._saml_allowed_user_ids()
                and user.password
            )
            if saml_users:
                # same error as an api.constrains because it is a constraint.
                # a standard constrains would require the use of SQL as the password
                # field is obfuscated by the base module.
                raise ValidationError(
                    _(
                        "This database disallows users to "
                        "have both passwords and SAML IDs. "
                        "Error for logins %s"
                    )
                    % saml_users.mapped("login")
                )
        # handle setting password to NULL
        blank_password_users = self.filtered(lambda user: user.password is False)
        non_blank_password_users = self - blank_password_users
        if non_blank_password_users:
            # pylint: disable=protected-access
            super(ResUser, non_blank_password_users)._set_password()
        if blank_password_users:
            # similar to what Odoo does in Users._set_encrypted_password
            self.env.cr.execute(
                "UPDATE res_users SET password = NULL WHERE id IN %s",
                (tuple(blank_password_users.ids),),
            )
            self.invalidate_cache(["password"], blank_password_users.ids)
        return

    def _auth_saml_validate(self, provider_id, token, base_url=None):
        provider = self.env["auth.saml.provider"].sudo().browse(provider_id)
        return provider._validate_auth_response(token, base_url)

    def _auth_saml_signin(self, provider, validation, saml_response):
        """retrieve and sign into openerp the user corresponding to provider
        and validated access token

        :param provider: saml provider id (int)
            :param validation: result of validation of access token (dict)
            :param saml_response: saml parameters response from the IDP
            :return: user login (str)
            :raise: openerp.exceptions.AccessDenied if signin failed

            This method can be overridden to add alternative signin methods.
        """
        token_osv = self.env["auth_saml.token"]
        saml_uid = validation["user_id"]
        user = (
            self.env["res.users.saml"]
            .search(
                [("saml_uid", "=", saml_uid), ("saml_provider_id", "=", provider)],
                limit=1,
            )
            .user_id
        )
        if len(user) != 1:
            raise AccessDenied()
        # now find if a token for this user/provider already exists
        token_ids = token_osv.search(
            [("saml_provider_id", "=", provider), ("user_id", "=", user.id)]
        )

        if token_ids:
            token_ids.write({"saml_access_token": saml_response})
        else:
            _logger.info("Creating auth_saml.token")
            token_ids = token_osv.create(
                {
                    "saml_access_token": saml_response,
                    "saml_provider_id": provider,
                    "user_id": user.id,
                }
            )

        if validation.get("mapped_attrs", {}):
            user.write(validation.get("mapped_attrs", {}))

        return user.login

    @api.model
    def auth_saml(self, provider, saml_response, base_url=None):
        validation = self._auth_saml_validate(provider, saml_response, base_url)

        # required check
        if not validation.get("user_id"):
            raise AccessDenied()

        # retrieve and sign in user
        login = self._auth_saml_signin(provider, validation, saml_response)

        if not login:
            raise AccessDenied()

        # return user credentials
        return self.env.cr.dbname, login, saml_response

    def _check_credentials(self, password, env):
        """Override to handle SAML auths.

        The token can be a password if the user has used the normal form...
        but we are more interested in the case when they are tokens
        and the interesting code is inside the "except" clause.
        """
        try:
            # Attempt a regular login (via other auth addons) first.
            return super()._check_credentials(password, env)

        except (AccessDenied, passlib.exc.PasswordSizeError):
            # since normal auth did not succeed we now try to find if the user
            # has an active token attached to his uid
            token = (
                self.env["auth_saml.token"]
                .sudo()
                .search(
                    [
                        ("user_id", "=", self.env.user.id),
                        ("saml_access_token", "=", password),
                    ]
                )
            )
            if not token:
                raise AccessDenied()

    def _autoremove_password_if_saml(self):
        """Helper to remove password if it is forbidden for SAML users."""
        if self.env.context.get("auth_saml_no_autoremove_password"):
            return
        if self._allow_saml_and_password():
            return
        to_remove_password = self.filtered(
            lambda rec: rec.id != SUPERUSER_ID and rec.saml_ids and rec.password
        )
        if to_remove_password:

            to_remove_password.with_context(
                auth_saml_no_autoremove_password=True
            ).write({"password": self._autoremove_password_if_saml_gen_password()})

    @api.model
    def _autoremove_password_if_saml_gen_password(self):
        """
        If password_security is installed, we cannot have a False-y password.
        This is to avoid a very small bridging/compatbility module.
        """
        new_password = False
        modules = self.env["ir.module.module"].sudo()._installed().keys()
        if "password_security" in modules:
            new_password = passlib.utils.generate_password(size=24, charset="ascii_72")
        return new_password

    def write(self, vals):
        result = super().write(vals)
        self._autoremove_password_if_saml()
        self._ensure_saml_token_exists()
        return result

    @api.model_create_multi
    def create(self, vals_list):
        result = super().create(vals_list)
        result._autoremove_password_if_saml()
        result._ensure_saml_token_exists()
        return result

    @api.model
    def _allow_saml_and_password(self):
        """Can both SAML and local password auth methods can coexist."""
        return tools.str2bool(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("auth_saml.allow_saml_uid_and_internal_password", "False")
        )

    def _ensure_saml_token_exists(self):
        # workaround for Odoo not seeing the newly created auth_saml.token on
        # the very first login
        model_token = self.env["auth_saml.token"].sudo()
        for record in self.sudo().filtered(lambda r: r.saml_ids):
            for provider in record.saml_ids:
                token = model_token.search(
                    [
                        ("user_id", "=", record.id),
                        ("saml_provider_id", "=", provider.saml_provider_id.id),
                    ]
                )
                if not token:
                    model_token.create(
                        {
                            "user_id": record.id,
                            "saml_provider_id": provider.saml_provider_id.id,
                        }
                    )
