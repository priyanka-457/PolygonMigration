from azure.identity import ClientSecretCredential
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ClientAuthenticationError
import logging

logger = logging.getLogger(__name__)


class AzureBlobManager:
    """
    Handles read/write operations for Azure Blob Storage using
    an Azure Entra ID application (service principal).
    """

    def __init__(self, account_url, tenant_id, client_id, client_secret):
        """
        Initializes the Blob Service Client using an application
        client ID and client secret.

        Args:
            account_url (str): Azure Storage Account URL.
            tenant_id (str): Azure Entra ID tenant ID.
            client_id (str): Application (client) ID.
            client_secret (str): Application client secret.
        """
        self.account_url = account_url
        self.blob_service_client = None

        logger.info("Attempting to authenticate with Azure...")

        try:
            credential = ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )

            self.blob_service_client = BlobServiceClient(
                account_url=self.account_url,
                credential=credential,
            )

            logger.info(
                "Authentication successful. BlobServiceClient created."
            )

        except ClientAuthenticationError as e:
            logger.error("Azure authentication failed: %s", e)
            raise

        except Exception as e:
            logger.error(
                "Unexpected error during Azure initialization: %s",
                e,
            )
            raise

    def upload_test_case(
        self,
        container_name,
        db_problem_id,
        test_number,
        input_data,
        output_data,
    ):
        """
        Uploads a test case's input and output as separate blobs.

        Required structure:

            test_cases/{problem_id}/{test_number}
            test_cases/{problem_id}/{test_number}.a
        """

        input_blob_name = f"test_cases/{db_problem_id}/{test_number}"
        output_blob_name = f"test_cases/{db_problem_id}/{test_number}.a"

        try:
            input_blob_client = self.blob_service_client.get_blob_client(
                container=container_name,
                blob=input_blob_name,
            )

            input_blob_client.upload_blob(
                input_data.encode("utf-8"),
                overwrite=True,
            )

            logger.info(
                "Uploaded input for test #%s to %s",
                test_number,
                input_blob_name,
            )

            output_blob_client = self.blob_service_client.get_blob_client(
                container=container_name,
                blob=output_blob_name,
            )

            output_blob_client.upload_blob(
                output_data.encode("utf-8"),
                overwrite=True,
            )

            logger.info(
                "Uploaded output for test #%s to %s",
                test_number,
                output_blob_name,
            )

            return {
                "input": input_blob_name,
                "output": output_blob_name,
            }

        except Exception as e:
            logger.error(
                "Error uploading test case #%s: %s",
                test_number,
                e,
            )
            raise

    def upload_bytes(self, container_name, blob_name, data):
        """
        Uploads arbitrary bytes to a blob.

        Used by the storage abstraction for files such as
        custom checkers.
        """

        try:
            blob_client = self.blob_service_client.get_blob_client(
                container=container_name,
                blob=blob_name,
            )

            blob_client.upload_blob(
                data,
                overwrite=True,
            )

            logger.info(
                "Uploaded blob %s to container %s",
                blob_name,
                container_name,
            )

            return blob_name

        except Exception as e:
            logger.error(
                "Error uploading blob %s: %s",
                blob_name,
                e,
            )
            raise

    def empty_blob(self, container_name, problem_id):
        """
        Deletes all blobs for a given problem ID.
        """

        prefix = f"test_cases/{problem_id}/"

        try:
            container_client = self.blob_service_client.get_container_client(
                container_name
            )

            blobs_to_delete = [
                blob.name
                for blob in container_client.list_blobs(
                    name_starts_with=prefix
                )
            ]

            for blob_name in blobs_to_delete:
                logger.info("Deleting blob: %s", blob_name)
                container_client.delete_blob(blob_name)

            logger.info(
                "Deleted %s blobs for problem %s in container %s.",
                len(blobs_to_delete),
                problem_id,
                container_name,
            )

            return blobs_to_delete

        except Exception as e:
            logger.error(
                "Error deleting blobs for problem %s: %s",
                problem_id,
                e,
            )
            raise