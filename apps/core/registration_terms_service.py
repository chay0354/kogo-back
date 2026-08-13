from apps.core.models import RegistrationTerms
from apps.core.registration_terms_default import DEFAULT_REGISTRATION_TERMS_HTML


def get_registration_terms() -> RegistrationTerms:
    obj, _ = RegistrationTerms.objects.get_or_create(
        pk=1,
        defaults={'content': DEFAULT_REGISTRATION_TERMS_HTML},
    )
    return obj
