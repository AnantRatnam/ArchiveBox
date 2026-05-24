__package__ = "archivebox.misc"

from django.core.paginator import Paginator
from django.db import connection
from django.utils.functional import cached_property


class AcceleratedPaginator(Paginator):
    """
    Accelerated paginator ignores DISTINCT when counting total number of rows.
    Speeds up SELECT Count(*) on Admin views by >20x.
    https://hakibenita.com/optimizing-the-django-admin-paginator
    """

    @cached_property
    def count(self):
        query = getattr(self.object_list, "query", None)
        if query is not None and (getattr(query, "distinct", False) or getattr(getattr(query, "where", None), "children", None)):
            # fallback to normal count method on filtered queryset
            return super().count

        model = getattr(self.object_list, "model", None)
        if model is None:
            return super().count

        # otherwise count total rows in a separate fast query
        if connection.vendor == "sqlite":
            table_name = model._meta.db_table
            with connection.cursor() as cursor:
                try:
                    cursor.execute("SELECT stat FROM sqlite_stat1 WHERE tbl = %s", [table_name])
                    stats = [int(str(row[0]).split()[0]) for row in cursor.fetchall() if row and row[0]]
                except Exception:
                    stats = []
            if stats:
                return max(stats)

        return model.objects.count()

        # Alternative approach for PostgreSQL: fallback count takes > 200ms
        # from django.db import connection, transaction, OperationalError
        # with transaction.atomic(), connection.cursor() as cursor:
        #     cursor.execute('SET LOCAL statement_timeout TO 200;')
        #     try:
        #         return super().count
        #     except OperationalError:
        #         return 9999999999999
