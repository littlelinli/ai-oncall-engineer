def lambda_handler(event, context):
    print("Alarm event received:", event)
    # TODO 1: read recent logs and metrics from CloudWatch
    # TODO 2: send them to the Gemini API and get the analysis
    # TODO 3: save the report to DynamoDB and S3, send an email with SNS
    return {"status":"ok"}
