import jsii
from aws_cdk import Annotations, CfnResource, IAspect, TagManager
from constructs import IConstruct

# Module-level constant — used by tests and __init__.py exports
REQUIRED_TAG_KEYS = [
    "ClarityID",
    "DataProductName",
    "DataProductId",
    "Environment",
    "Component",
    "BU",
]


@jsii.implements(IAspect)
class CdkTaggingAspect:
    REQUIRED_TAGS = REQUIRED_TAG_KEYS  # alias for backward compat

    def __init__(self, tags: dict, environment: str):
        self.tags = dict(tags or {})
        self.environment = environment

        # Inject Environment automatically from CDK context/env if missing
        self.tags.setdefault("Environment", environment)

    def visit(self, node: IConstruct) -> None:
        if not self._is_taggable_cfn_resource(node):
            return

        missing_tags = [
            tag_key
            for tag_key in self.REQUIRED_TAGS
            if tag_key not in self.tags or str(self.tags.get(tag_key, "")).strip() == ""
        ]

        if missing_tags:
            Annotations.of(node).add_error(
                "[TaggingPolicy] Resource "
                f"'{node.node.path}' of type '{node.cfn_resource_type}' "
                "is missing required cost-allocation tag(s): "
                f"{', '.join(missing_tags)}"
            )
            return

        for tag_key in self.REQUIRED_TAGS:
            # Set directly on the resource's TagManager — avoids scheduling
            # a nested Tag Aspect which causes priority ordering conflicts
            node.tags.set_tag(tag_key, str(self.tags[tag_key]))

    @staticmethod
    def _is_taggable_cfn_resource(node: IConstruct) -> bool:
        return isinstance(node, CfnResource) and isinstance(
            getattr(node, "tags", None), TagManager
        )
