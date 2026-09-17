/**
 * One definition of "stateful" for the two layers that act on it.
 *
 * The membership rule is what three years of deployment history on the real clusters shows has
 * never been replaced by an upgrade. Compute instances and load-balancer target groups are
 * deliberately absent: both are replaced routinely and by design, so marking them Retain would
 * leave an orphan every time rather than protecting anything. Nothing here has ever been lost.
 * This is insurance against the first plausible occasion, which is a control-plane cutover.
 *
 * The synthesis marks these resources `UpdateReplacePolicy: Retain` (`RetainStatefulOnUpdateReplace`
 * in `app.ts`), so an update that forces a replacement orphans the old resource instead of
 * deleting it. The deploy-time change-set guard
 * in `src/cli/cdk-invoker.ts` refuses a `Remove` of the same set. Both import from here, so the
 * two layers cannot come to disagree about what counts as stateful.
 *
 * Membership is by CloudFormation namespace rather than by exact type, so a type added to one of
 * these services later is covered without anyone remembering to add it. The cost is that the
 * association and policy members of those namespaces are covered too, and one of those retained
 * is litter rather than a saved copy. That trade is deliberate: an orphan can be inspected and
 * deleted by hand, a deleted file system cannot be recovered at all.
 *
 * `OpenSearchService`/`Elasticsearch` and `KinesisFirehose` are the same services under their two
 * CloudFormation namespaces; both spellings are listed so a template that uses the older one is
 * still covered.
 *
 * This module holds no imports on purpose: the command line imports it too, and it must not pull
 * the CDK library into a process that only reads a change set.
 */

export const STATEFUL_TYPE_PREFIXES: readonly string[] = [
  // The identity store, the directory, the file systems, the search domain, the streams, the
  // secrets, the DNS zone and records, the queues, the topics and the logs.
  'AWS::Cognito::',
  'AWS::DirectoryService::',
  'AWS::EFS::',
  'AWS::FSx::',
  'AWS::OpenSearchService::',
  'AWS::OpenSearchServerless::',
  'AWS::Elasticsearch::',
  'AWS::Kinesis::',
  'AWS::KinesisFirehose::',
  'AWS::SecretsManager::',
  'AWS::Route53::',
  'AWS::SQS::',
  'AWS::SNS::',
  'AWS::Logs::',
  // A vault holds recovery points, and a plan or selection that stops existing stops producing
  // them.
  'AWS::Backup::',
  // Not built by any stack today. Listed so the first one that is arrives protected, because
  // these are the namespaces where a deletion is unrecoverable by definition.
  'AWS::S3::Bucket',
  'AWS::DynamoDB::',
  'AWS::RDS::',
  'AWS::EC2::Volume',
  'AWS::KMS::Key',
];

export function isStatefulType(resourceType: string | undefined): boolean {
  if (resourceType === undefined) return false;
  return STATEFUL_TYPE_PREFIXES.some((prefix) => resourceType.startsWith(prefix));
}
