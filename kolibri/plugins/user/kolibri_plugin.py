from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

from kolibri.core.auth.constants.user_kinds import ANONYMOUS
from kolibri.core.device.utils import is_landing_page
from kolibri.core.device.utils import LANDING_PAGE_LEARN
from kolibri.core.hooks import NavigationHook
from kolibri.core.hooks import RoleBasedRedirectHook
from kolibri.core.oidc_provider_hook import OIDCProviderHook
from kolibri.core.webpack import hooks as webpack_hooks
from kolibri.plugins import KolibriPluginBase
from kolibri.plugins.hooks import register_hook


class User(KolibriPluginBase):
    translated_view_urls = "urls"


@register_hook
class UserAsset(webpack_hooks.WebpackBundleHook):
    bundle_id = "app"

    @property
    def plugin_data(self):
        from kolibri.core.auth.models import FacilityUser

        def sanitize_users(user):
            return {
                "id": user.id,
                "username": user.username,
                "facility_id": user.facility_id,
                "is_learner": len(user.get_roles_for_user(user)) == 0,
                "needs_password": (
                    user.password == "NOT_SPECIFIED" or not user.password
                ),
            }

        return {
            "oidcProviderEnabled": OIDCProviderHook.is_enabled(),
            "deviceUsers": list(map(sanitize_users, FacilityUser.objects.all())),
        }


@register_hook
class LogInRedirect(RoleBasedRedirectHook):
    @property
    def roles(self):
        if is_landing_page(LANDING_PAGE_LEARN):
            return (None,)

        return (ANONYMOUS,)

    @property
    def url(self):
        return self.plugin_url(User, "user")


@register_hook
class LogInNavAction(NavigationHook):
    bundle_id = "login_side_nav"


@register_hook
class ProfileNavAction(NavigationHook):
    bundle_id = "user_profile_side_nav"
