data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "app" {
  count = var.enable_ec2 ? 1 : 0

  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
  }

  # Installs the demo app (systemd, port 8080) and CloudWatch Agent.
  # Install log on the instance: /var/log/cloud-init-output.log
  user_data                   = file("${path.module}/../scripts/setup.sh")
  user_data_replace_on_change = true

  # A new AMI release must not recreate the instance with the app on it
  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "incident-demo-app" }
}

# The instance was created before enable_ec2 existed
moved {
  from = aws_instance.app
  to   = aws_instance.app[0]
}
