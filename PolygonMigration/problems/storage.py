from abc import ABC, abstractmethod

from django.conf import settings

from .AzureTestcase import AzureBlobManager


class StorageProvider(ABC):
    @abstractmethod
    def upload_test_case(
        self,
        container_name,
        db_problem_id,
        test_number,
        input_data,
        output_data,
    ):
        raise NotImplementedError

    @abstractmethod
    def empty_problem(self, container_name, problem_id):
        raise NotImplementedError

    @abstractmethod
    def upload_bytes(self, container_name, blob_name, data):
        raise NotImplementedError


class AzureStorageProvider(StorageProvider):
    def __init__(
        self,
        account_url,
        tenant_id,
        client_id,
        client_secret,
    ):
        self.manager = AzureBlobManager(
            account_url=account_url,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    def upload_test_case(
        self,
        container_name,
        db_problem_id,
        test_number,
        input_data,
        output_data,
    ):
        return self.manager.upload_test_case(
            container_name=container_name,
            db_problem_id=db_problem_id,
            test_number=test_number,
            input_data=input_data,
            output_data=output_data,
        )

    def empty_problem(self, container_name, problem_id):
        return self.manager.empty_blob(
            container_name=container_name,
            problem_id=problem_id,
        )

    def upload_bytes(self, container_name, blob_name, data):
        return self.manager.upload_bytes(
            container_name=container_name,
            blob_name=blob_name,
            data=data,
        )


def get_storage_provider():
    provider = (
        getattr(settings, "STORAGE_PROVIDER", "") or ""
    ).strip().lower()

    if provider == "azure":
        required_settings = {
            "AZURE_STORAGE_ACCOUNT_URL": settings.AZURE_STORAGE_ACCOUNT_URL,
            "AZURE_TENANT_ID": settings.AZURE_TENANT_ID,
            "AZURE_CLIENT_ID": settings.AZURE_CLIENT_ID,
            "AZURE_CLIENT_SECRET": settings.AZURE_CLIENT_SECRET,
        }

        missing = [
            name
            for name, value in required_settings.items()
            if not value
        ]

        if missing:
            raise ValueError(
                "Missing Azure storage configuration: "
                + ", ".join(missing)
            )

        return AzureStorageProvider(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            tenant_id=settings.AZURE_TENANT_ID,
            client_id=settings.AZURE_CLIENT_ID,
            client_secret=settings.AZURE_CLIENT_SECRET,
        )

    raise ValueError(
        f"Unsupported STORAGE_PROVIDER: '{provider}'. "
        "Supported providers: azure"
    )