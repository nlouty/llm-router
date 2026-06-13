from __future__ import annotations

from django.db.models import F

from router.models import Model


class ModelRepository:
    @staticmethod
    def get_by_name(model_name: str | None) -> Model | None:
        if not model_name:
            return None
        return Model.objects.filter(model_name=model_name).first()

    @staticmethod
    def is_auto_model_name(model_name: str | None) -> bool:
        return isinstance(model_name, str) and model_name.casefold() == "auto"

    @staticmethod
    def should_auto_select(model: Model | None) -> bool:
        return bool(model and model.auto)

    @staticmethod
    def get_or_create(model_name: str) -> tuple[Model, bool]:
        return Model.objects.get_or_create(
            model_name=model_name,
            defaults={"concurrent_limit": 3, "max_tokens": 20480},
        )

    @staticmethod
    def list_all() -> list[Model]:
        return list(Model.objects.all().order_by("id"))

    @staticmethod
    def list_auto_selectable_models() -> list[Model]:
        return list(
            Model.objects.filter(
                deprecation__isnull=True,
                complexity_min__isnull=False,
                complexity_max__isnull=False,
                complexity_min__gte=1,
                complexity_max__lte=10,
                complexity_min__lte=F("complexity_max"),
            ).order_by("id")
        )

    @staticmethod
    def get_routing_models() -> list[Model]:
        return list(Model.objects.filter(is_routing_model=True).order_by("id"))

    @staticmethod
    def get_multimodal_model() -> Model | None:
        return Model.objects.filter(
            multimodal=True,
            deprecation__isnull=True,
        ).order_by("id").first()

    @staticmethod
    def get_by_names(model_names: list[str]) -> dict[str, Model]:
        return {model.model_name: model for model in Model.objects.filter(model_name__in=model_names)}

    @staticmethod
    def list_online() -> list[Model]:
        """List all models that are not deprecated (deprecation is null)."""
        return list(Model.objects.filter(deprecation__isnull=True).order_by("id"))
