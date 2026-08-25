# Generated manually for LibreLane web app

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FlowRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("setting_up", "Setting up PDK"),
                            ("ready", "Ready"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("design_name", models.CharField(default="spm", max_length=128)),
                ("pdk", models.CharField(default="sky130A", max_length=64)),
                ("pdk_family", models.CharField(default="sky130", max_length=64)),
                ("pdk_root", models.CharField(default="~/.ciel", max_length=512)),
                ("clock_period", models.FloatField(default=10.0)),
                ("work_dir", models.CharField(blank=True, max_length=1024)),
                ("current_step_index", models.IntegerField(default=-1)),
                ("error_message", models.TextField(blank=True)),
                ("setup_log", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="FlowStepResult",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("order", models.PositiveIntegerField()),
                ("step_id", models.CharField(max_length=128)),
                ("title", models.CharField(max_length=256)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("log", models.TextField(blank=True)),
                ("summary", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="flow.flowrun",
                    ),
                ),
            ],
            options={
                "ordering": ["order"],
                "unique_together": {("run", "order")},
            },
        ),
    ]
