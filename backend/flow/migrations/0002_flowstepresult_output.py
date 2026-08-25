from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("flow", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="flowstepresult",
            name="output",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
