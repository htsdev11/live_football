"""Explicit pagination for APIView list handlers.

APIView does not paginate automatically, so every list view instantiates one of
these classes and calls ``paginate_queryset`` / ``get_paginated_response``.
Responses are always ``{count, next, previous, results}``.
"""

from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """Default: 50 per page, hard ceiling of 200. No unbounded collections."""

    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200
    page_query_param = 'page'


class SmallPagination(StandardPagination):
    """For child collections such as events, shots or commentary."""

    page_size = 100
    max_page_size = 200
