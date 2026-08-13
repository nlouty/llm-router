from __future__ import annotations

from collections.abc import Iterable

from django.db.models import F

from router.models import Model, Server


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
    def list_active_models() -> list[Model]:
        return list(Model.objects.filter(deprecation__isnull=True).order_by("id"))

    @staticmethod
    def list_auto_selectable_models() -> list[Model]:
        # Deprecation is intentionally ignored here: a model with complexity
        # bounds is an auto-routing target even when deprecated by name, so a
        # deprecated model can still serve `auto` requests.
        return list(
            Model.objects.filter(
                complexity_min__isnull=False,
                complexity_max__isnull=False,
                complexity_min__gte=1,
                complexity_max__lte=10,
                complexity_min__lte=F("complexity_max"),
            ).order_by("id")
        )

    @staticmethod
    def get_auto_model_for_complexity(complexity: int) -> Model | None:
        candidates = ModelRepository.list_auto_selectable_models()
        matching = [
            model for model in candidates
            if model.complexity_min <= complexity <= model.complexity_max
        ]
        return matching[0] if len(matching) == 1 else None

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
    def get_by_ids(model_ids: Iterable[int]) -> dict[int, Model]:
        return {model.id: model for model in Model.objects.filter(id__in=list(model_ids))}

    @staticmethod
    def list_online() -> list[Model]:
        """Models shown in the dashboard: models backed by at least one online server.

        Based on the servers table rather than ``deprecation`` so that a model
        whose ``deprecation`` is set as an access-control word (blocking normal
        users by name, while still serving VIP / auto requests) is not hidden
        from the dashboard as long as it has live servers behind it.
        """
        online_model_ids = (
            Server.objects.filter(deleted_at__isnull=True, is_online=True)
            .values_list("model_id", flat=True)
            .order_by("model_id")
            .distinct()
        )
        return list(Model.objects.filter(id__in=online_model_ids).order_by("id"))
