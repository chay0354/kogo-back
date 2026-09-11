import base64

from rest_framework import serializers

from apps.signatures.models import Signature
from apps.signatures.text import html_to_paragraphs


def signature_pdf_path(signature) -> str:
    """Relative to the API base (/api/v1), like the download_url of the child's documents."""
    return f'/signatures/{signature.id}/pdf/'


class SignatureListSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source='get_kind_display', read_only=True)
    family_id = serializers.UUIDField(read_only=True, allow_null=True)
    family_name = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Signature
        fields = [
            'id', 'kind', 'kind_label', 'signed_at',
            'signer_name', 'signer_id_number',
            'family_id', 'family_name', 'children', 'branch_name',
            'document_title', 'document_sha256', 'consents', 'pdf_url',
        ]
        read_only_fields = fields

    def get_family_name(self, obj):
        return obj.family.name if obj.family_id else None

    def get_children(self, obj):
        return [{'id': str(child.id), 'full_name': child.full_name} for child in obj.children.all()]

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else None

    def get_pdf_url(self, obj):
        return signature_pdf_path(obj)


class SignatureDetailSerializer(SignatureListSerializer):
    document_text = serializers.SerializerMethodField()
    signature_image = serializers.SerializerMethodField()

    class Meta(SignatureListSerializer.Meta):
        fields = SignatureListSerializer.Meta.fields + [
            'document_text', 'signature_image', 'ip_address', 'user_agent', 'refs',
        ]
        read_only_fields = fields

    def get_document_text(self, obj):
        return html_to_paragraphs(obj.document_html)

    def get_signature_image(self, obj):
        png = bytes(obj.signature_png or b'')
        if not png:
            return None
        return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
