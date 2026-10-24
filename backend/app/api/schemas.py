from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MethodName = Literal["quick", "container", "full"]


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=4096)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=200)
    enabled: bool = True


class LibraryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    exclude_patterns: list[str] | None = Field(None, max_length=200)
    enabled: bool | None = None


class JobCreate(BaseModel):
    task: Literal["incremental", "new", "changed", "full", "errors", "files"] = "incremental"
    methods: list[MethodName] = Field(min_length=1, max_length=3)
    library_ids: list[int] | None = Field(None, max_length=1000)
    file_ids: list[int] | None = Field(None, max_length=100000)
    priority: int = Field(0, ge=-100, le=100)
    force: bool | None = None
    name: str | None = Field(None, max_length=300)


class JobUpdate(BaseModel):
    priority: int = Field(ge=-100, le=100)


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    kind: Literal["daily", "weekly", "monthly", "custom"]
    time: str | None = Field(None, pattern=r"^\d{1,2}:\d{2}$")
    weekday: int | None = Field(None, ge=0, le=6)
    day: int | None = Field(None, ge=1, le=28)
    cron: str | None = Field(None, max_length=128)
    task: Literal["new", "changed", "new_changed", "full", "errors"]
    methods: list[MethodName] = Field(min_length=1, max_length=3)
    library_ids: list[int] = Field(default_factory=list, max_length=1000)
    priority: int = Field(0, ge=-100, le=100)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class PasswordIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    enable: bool = True


class RepairIn(BaseModel):
    action: Literal["remux"] = "remux"
    confirm: bool = False
    confirm_filename: str | None = Field(None, max_length=1024)
