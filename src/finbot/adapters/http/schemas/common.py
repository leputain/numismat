from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiInfo(ApiModel):
    service: Literal["numismat"] = "numismat"
    api_version: Literal["v1"] = "v1"


class HealthStatus(ApiModel):
    status: Literal["ok"] = "ok"


class ApiError(ApiModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=160)
    details: dict[str, int] = Field(default_factory=dict)


class ApiErrorResponse(ApiModel):
    error: ApiError
