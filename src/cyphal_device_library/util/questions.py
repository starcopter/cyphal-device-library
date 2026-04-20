"""Utility classes for questions."""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Optional

import questionary


class Question(ABC):
    """Abstract base class for questions."""

    def __init__(
        self,
        message: str,
        instruction: str | None = None,
        default: Optional[str | bool] = None,
    ):
        self.message: str = message
        self.instruction: str | None = instruction
        self.default: Optional[str | bool] = default
        self.question_type: str = "text"

    @abstractmethod
    async def ask(self) -> str | bool:
        """Ask the question and return the answer."""
        pass


class TextQuestion(Question):
    """Free text input question."""

    def __init__(
        self,
        message: str,
        instruction: str | None = None,
        default: Optional[str] = None,
        validate: Optional[Callable[[str], str | bool]] = None,
    ):
        super().__init__(message, instruction, default)
        self.question_type = "text"
        self.validate: Optional[Callable[[str], str | bool]] = validate

    async def ask(self) -> str:
        question = questionary.text(
            message=self.message,
            instruction=self.instruction,
            default=self.default if isinstance(self.default, str) else "",
            validate=self.validate,
        )
        return await question.ask_async()


class PasswordQuestion(Question):
    """Password input question."""

    @staticmethod
    def validate_password(password: str) -> str | bool:
        if len(password) < 6:
            return "Password must be at least 6 characters"
        elif not re.search("[0-9]", password) and not re.search("[A-Z]", password):
            return "Password must contain a number or an upper-case letter"
        elif not re.search("[a-z]", password):
            return "Password must contain a lower-case letter"
        return True

    def __init__(
        self,
        message: str,
        instruction: str | None = None,
        default: Optional[str] = None,
        validate: Optional[Callable[[str], str | bool]] = None,
    ):
        super().__init__(message, instruction, default)
        self.question_type = "password"
        self.validate: Optional[Callable[[str], str | bool]] = validate or self.validate_password

    async def ask(self) -> str:
        question = questionary.password(
            message=self.message,
            instruction=self.instruction,
            default=self.default if isinstance(self.default, str) else "",
            validate=self.validate,
        )
        return await question.ask_async()


class SelectQuestion(Question):
    """Question with multiple choice options."""

    def __init__(
        self,
        message: str,
        instruction: str | None = None,
        choices: list[str] = [],
        default: Optional[str] = None,
        use_shortcuts: bool = False,
    ):
        super().__init__(message, instruction, default)
        self.choices: list[str] | None = choices
        self.question_type = "select"
        self.use_shortcuts = use_shortcuts

    async def ask(self) -> str:
        question = questionary.select(
            message=self.message,
            instruction=self.instruction,
            choices=self.choices or [],
            default=self.default if isinstance(self.default, str) else None,
            use_shortcuts=self.use_shortcuts,
        )
        return await question.ask_async()


class ConfirmQuestion(Question):
    """Yes/no confirmation question."""

    def __init__(self, message: str, instruction: str | None = None, default: bool = True):
        super().__init__(message, instruction, default)
        self.question_type = "confirm"

    async def ask(self) -> bool:
        question = questionary.confirm(
            message=self.message,
            instruction=self.instruction,
            default=self.default if isinstance(self.default, bool) else True,
        )
        return await question.ask_async()
