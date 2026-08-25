from django.db import models


class FlowRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SETTING_UP = "setting_up", "Setting up PDK"
        READY = "ready", "Ready"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    design_name = models.CharField(max_length=128, default="spm")
    pdk = models.CharField(max_length=64, default="sky130A")
    pdk_family = models.CharField(max_length=64, default="sky130")
    pdk_root = models.CharField(max_length=512, default="~/.ciel")
    clock_period = models.FloatField(default=10.0)
    work_dir = models.CharField(max_length=1024, blank=True)
    current_step_index = models.IntegerField(default=-1)
    error_message = models.TextField(blank=True)
    setup_log = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"FlowRun #{self.pk} ({self.status})"


class FlowStepResult(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    run = models.ForeignKey(FlowRun, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveIntegerField()
    step_id = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    log = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    output = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order"]
        unique_together = [("run", "order")]

    def __str__(self) -> str:
        return f"{self.step_id} ({self.status})"
