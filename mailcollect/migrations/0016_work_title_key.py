"""Work.title_key (2026-11-22): KT wrote one work's title as 'kt cloud …' and 'KT Cloud …', or '작업 (X)' and
'작업(X)', and each spelling became a work of its own. Fill the key and merge the works it shows to be one: the
oldest keeps its key; the others' notices, notifications (rounds renumbered by KT's send), closures and reworks
move to it."""
import re
import unicodedata
from collections import defaultdict

from django.db import migrations, models


def title_key(title):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", title).casefold())


def merge(apps, schema_editor):
    Work = apps.get_model("mailcollect", "Work")
    Notice = apps.get_model("mailcollect", "Notice")
    Notification = apps.get_model("notifications", "Notification")
    WorkClosure = apps.get_model("notifications", "WorkClosure")
    same = defaultdict(list)
    for work in Work.objects.order_by("pk"):
        work.title_key = title_key(work.title)
        work.save(update_fields=["title_key"])
        same[(work.incident, work.title_key, work.starts_at)].append(work)
    for keep, *others in (works for works in same.values() if len(works) > 1):
        ids = [w.pk for w in others]
        Notice.objects.filter(work_id__in=ids).update(work=keep)
        Work.objects.filter(rework_of_id__in=ids).update(rework_of=keep)
        for closure in WorkClosure.objects.filter(work_id__in=ids):
            if WorkClosure.objects.filter(work=keep, channel_id=closure.channel_id).exists():
                closure.delete()
            else:
                closure.work = keep
                closure.save(update_fields=["work"])
        if keep.rework_of_id in ids:
            keep.rework_of = None
            keep.save(update_fields=["rework_of"])
        moved = list(Notification.objects.filter(work_id__in=[keep.pk, *ids]).select_related("notice__seen"))
        # Each (account, kind) numbers its KT sends anew, oldest first; a send split by group shares its round.
        # Rounds go out of the way first so the unique (work, account, kind, round) never meets itself.
        for i, n in enumerate(moved, start=10000):
            n.round = i
            n.work = keep
            n.save(update_fields=["round", "work"])
        sends = defaultdict(dict)
        for n in sorted(moved, key=lambda n: (_sent(apps, n.notice), n.notice_id)):
            sends[(n.account, n.kind)].setdefault(n.notice_id, len(sends[(n.account, n.kind)]) + 1)
        for n in moved:
            n.round = sends[(n.account, n.kind)][n.notice_id]
            n.save(update_fields=["round"])
        Work.objects.filter(pk__in=ids).delete()


def _sent(apps, notice):
    MessageHeader = apps.get_model("mailcollect", "MessageHeader")
    header = MessageHeader.objects.filter(seen_id=notice.seen_id).first()
    return (header.date if header else None) or notice.seen.first_seen_at


class Migration(migrations.Migration):

    dependencies = [
        ("mailcollect", "0015_check"),
        ("notifications", "0018_postpone"),
    ]

    operations = [
        migrations.AddField(
            model_name="work",
            name="title_key",
            field=models.TextField(blank=True, db_index=True),
        ),
        migrations.RunPython(merge, migrations.RunPython.noop),
    ]
