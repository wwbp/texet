# -----------------------------------------------------------------------------
# ecr.tf — registry for the texet image the ECS tasks run.
# push-image.sh builds the repo Dockerfile and pushes ${image_tag} here.
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "texet" {
  name                 = local.name_prefix
  image_tag_mutability = "MUTABLE"
  force_delete         = true # disposable env — allow destroy with images present

  image_scanning_configuration {
    scan_on_push = false
  }

  tags = { Name = "${local.name_prefix}-ecr" }
}
