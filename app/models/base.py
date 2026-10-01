"""
BaseModel and Field abstraction supporting Pydantic v2 when installed,
with a robust fallback for environments without Pydantic.
"""

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union

T = TypeVar("T", bound="BaseModel")

try:
    import pydantic
    from pydantic import BaseModel as _PydanticBaseModel, Field as _PydanticField, ConfigDict as _PydanticConfigDict
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False


if HAS_PYDANTIC:
    BaseModel = _PydanticBaseModel
    Field = _PydanticField
    ConfigDict = _PydanticConfigDict

else:
    class FieldInfo:
        def __init__(
            self,
            default: Any = ...,
            default_factory: Optional[Callable[[], Any]] = None,
            description: Optional[str] = None,
            **kwargs: Any
        ):
            self.default = default
            self.default_factory = default_factory
            self.description = description
            self.extra = kwargs

    def Field(
        default: Any = ...,
        *,
        default_factory: Optional[Callable[[], Any]] = None,
        description: Optional[str] = None,
        **kwargs: Any
    ) -> Any:
        return FieldInfo(
            default=default,
            default_factory=default_factory,
            description=description,
            **kwargs
        )

    def ConfigDict(**kwargs: Any) -> Dict[str, Any]:
        return dict(**kwargs)

    class BaseModel:
        """
        Lightweight, genuine BaseModel implementation supporting Pydantic v2 syntax.
        """
        model_config: Dict[str, Any] = {}

        def __init__(self, **kwargs: Any):
            # Collect annotations and class attributes along MRO
            cls = self.__class__
            annotations: Dict[str, Any] = {}
            for base in reversed(cls.__mro__):
                annotations.update(getattr(base, "__annotations__", {}))

            # Initialize declared fields
            for name in annotations:
                if name.startswith("_"):
                    continue
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                elif hasattr(cls, name):
                    class_val = getattr(cls, name)
                    if isinstance(class_val, FieldInfo):
                        if class_val.default_factory is not None:
                            setattr(self, name, class_val.default_factory())
                        elif class_val.default is not ...:
                            setattr(self, name, class_val.default)
                        else:
                            setattr(self, name, None)
                    else:
                        setattr(self, name, class_val)
                else:
                    setattr(self, name, None)

            # Assign any extra kwargs
            for k, v in kwargs.items():
                if not hasattr(self, k):
                    setattr(self, k, v)

        def model_dump(self, mode: str = "python") -> Dict[str, Any]:
            """Recursively dumps model attributes to dictionary."""
            res: Dict[str, Any] = {}
            for k, v in self.__dict__.items():
                if k.startswith("_"):
                    continue
                res[k] = self._dump_value(v, mode=mode)
            return res

        @classmethod
        def _dump_value(cls, val: Any, mode: str = "python") -> Any:
            if isinstance(val, BaseModel):
                return val.model_dump(mode=mode)
            if isinstance(val, list):
                return [cls._dump_value(item, mode=mode) for item in val]
            if isinstance(val, dict):
                return {k: cls._dump_value(v, mode=mode) for k, v in val.items()}
            if isinstance(val, datetime) and mode == "json":
                return val.isoformat()
            if hasattr(val, "value"):  # Enums
                return val.value
            return val

        def dict(self) -> Dict[str, Any]:
            """Backwards compatible alias for model_dump()."""
            return self.model_dump()

        def model_dump_json(self) -> str:
            """Serializes model to JSON string."""
            return json.dumps(self.model_dump(mode="json"), default=str)

        def json(self) -> str:
            """Backwards compatible alias for model_dump_json()."""
            return self.model_dump_json()

        @classmethod
        def model_validate(cls: Type[T], obj: Any) -> T:
            """Instantiates model from a dictionary, mapping, or existing object."""
            if isinstance(obj, cls):
                return obj
            if isinstance(obj, dict):
                return cls(**obj)
            if hasattr(obj, "keys") and callable(getattr(obj, "keys")):
                return cls(**{k: obj[k] for k in obj.keys()})
            if hasattr(obj, "__dict__"):
                return cls(**{k: v for k, v in obj.__dict__.items() if not k.startswith("_")})
            raise ValueError(f"Cannot validate object of type {type(obj)} into {cls.__name__}")

        @classmethod
        def from_orm(cls: Type[T], obj: Any) -> T:
            return cls.model_validate(obj)

        def __repr__(self) -> str:
            fields = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items() if not k.startswith("_"))
            return f"{self.__class__.__name__}({fields})"

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, self.__class__):
                return False
            return self.model_dump() == other.model_dump()
