"""Role permissions: a token proves identity, not edit authority.

* reader (any authenticated user) - GET only
* staff editor (``is_staff``) - manual corrections, soft delete, sync run reads
* administrator (``is_superuser``) - overrides, user/token management, triggers
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffEditor(BasePermission):
    """Read for any authenticated user; writes require staff."""

    message = 'Staff privileges are required for this action.'

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class IsAdministrator(BasePermission):
    """Read for any authenticated user; writes require a superuser."""

    message = 'Administrator privileges are required for this action.'

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_superuser)


class IsStaffOnly(BasePermission):
    """Staff-only for every method, including reads (internal endpoints)."""

    message = 'This endpoint is restricted to staff users.'

    def has_permission(self, request, view) -> bool:
        return bool(request.user and request.user.is_staff)
