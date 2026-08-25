# Schema is owned by database/*.sql. These models are unmanaged (managed=False);
# this migration only syncs Django's migration state so migrate --check stays clean.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("username", models.CharField(max_length=100, unique=True)),
                ("password", models.CharField(max_length=100)),
                ("email", models.CharField(max_length=100)),
                ("session_nonce", models.TextField(blank=True, null=True)),
                ("first_name", models.CharField(blank=True, default="", max_length=100)),
                ("last_name", models.CharField(blank=True, default="", max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "users",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="FlowRun",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(default="spm", max_length=256)),
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
                ("work_dir", models.CharField(blank=True, default="", max_length=1024)),
                (
                    "temp_folder_name",
                    models.CharField(blank=True, default="", max_length=512),
                ),
                ("current_step_index", models.IntegerField(default=-1)),
                ("error_message", models.TextField(blank=True, default="")),
                ("setup_log", models.TextField(blank=True, default="")),
                ("artifacts_stored", models.BooleanField(default=False)),
                (
                    "owner_user",
                    models.ForeignKey(
                        db_column="owner_user_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        to="flow.user",
                    ),
                ),
            ],
            options={
                "db_table": "flow_runs",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="FlowStepResult",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("order", models.PositiveIntegerField(db_column="order")),
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
                ("log", models.TextField(blank=True, default="")),
                ("summary", models.TextField(blank=True, default="")),
                ("output", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "run",
                    models.ForeignKey(
                        db_column="run_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="flow.flowrun",
                    ),
                ),
            ],
            options={
                "db_table": "flow_step_results",
                "ordering": ["order"],
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="FlowRunFile",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("relative_path", models.CharField(max_length=1024)),
                ("content", models.BinaryField()),
                ("size_bytes", models.BigIntegerField(default=0)),
                (
                    "content_type",
                    models.CharField(
                        default="application/octet-stream", max_length=128
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "run",
                    models.ForeignKey(
                        db_column="run_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="files",
                        to="flow.flowrun",
                    ),
                ),
            ],
            options={
                "db_table": "flow_run_files",
                "managed": False,
                "unique_together": {("run", "relative_path")},
            },
        ),
    ]
