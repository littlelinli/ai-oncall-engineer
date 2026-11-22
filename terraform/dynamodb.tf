resource "aws_dynamodb_table" "incidents" {
  name         = "incident-demo-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  attribute {
    name = "incident_id"
    type = "S"
  }

  # The Lambda sets ttl = now + 7 days, DynamoDB deletes the item after that
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
