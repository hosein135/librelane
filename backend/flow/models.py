from django.db import models


class User(models.Model):
    id = models.BigAutoField(primary_key=True)
    username = models.CharField(max_length=100, unique=True)
    password = models.CharField(max_length=100)
    email = models.CharField(max_length=100)
    session_nonce = models.TextField(null=True, blank=True)
    first_name = models.CharField(max_length=100, default="", blank=True)
    last_name = models.CharField(max_length=100, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users"
        managed = False

    def __str__(self) -> str:
        return self.username


class FlowRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SETTING_UP = "setting_up", "Setting up PDK"
        READY = "ready", "Ready"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.BigAutoField(primary_key=True)
    owner_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column="owner_user_id",
        related_name="runs",
    )
    name = models.CharField(max_length=256, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    design_name = models.CharField(
        max_length=128,
        default="",
        help_text="Top Verilog module name (from uploaded sources).",
    )
    pdk = models.CharField(max_length=64, default="sky130A")
    pdk_family = models.CharField(max_length=64, default="sky130")
    pdk_root = models.CharField(max_length=512, default="~/.ciel")
    clock_period = models.FloatField(default=10.0)
    work_dir = models.CharField(max_length=1024, blank=True, default="")
    temp_folder_name = models.CharField(max_length=512, blank=True, default="")
    current_step_index = models.IntegerField(default=-1)
    error_message = models.TextField(blank=True, default="")
    setup_log = models.TextField(blank=True, default="")
    artifacts_stored = models.BooleanField(default=False)
    disk_bytes = models.BigIntegerField(default=0)
    db_bytes = models.BigIntegerField(default=0)

    class Meta:
        db_table = "flow_runs"
        managed = False

    def __str__(self) -> str:
        return f"FlowRun #{self.pk} ({self.status})"

    @property
    def get_status_display(self) -> str:
        return dict(self.Status.choices).get(self.status, self.status)


class FlowStepResult(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(
        FlowRun,
        on_delete=models.CASCADE,
        db_column="run_id",
        related_name="steps",
    )
    order = models.PositiveIntegerField(db_column="order")
    step_id = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    log = models.TextField(blank=True, default="")
    summary = models.TextField(blank=True, default="")
    output = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "flow_step_results"
        managed = False
        ordering = ["order"]

    def __str__(self) -> str:
        return f"{self.step_id} ({self.status})"

    @property
    def get_status_display(self) -> str:
        return dict(self.Status.choices).get(self.status, self.status)


class FlowRunFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(
        FlowRun,
        on_delete=models.CASCADE,
        db_column="run_id",
        related_name="files",
    )
    relative_path = models.CharField(max_length=1024)
    content = models.BinaryField()
    size_bytes = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=128, default="application/octet-stream")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flow_run_files"
        managed = False
        unique_together = (("run", "relative_path"),)

    def __str__(self) -> str:
        return f"{self.run_id}:{self.relative_path}"
