// NpcBrain.cpp — Unreal 客户端实现（详见 .h 用法说明）
#include "NpcBrain.h"
#include "HttpModule.h"
#include "Interfaces/IHttpRequest.h"
#include "Interfaces/IHttpResponse.h"
#include "Serialization/JsonSerializer.h"
#include "Kismet/GameplayStatics.h"

UNpcBrain::UNpcBrain()
{
	PrimaryComponentTick.bCanEverTick = true;
}

void UNpcBrain::Talk(const FString& Message)
{
	TSharedRef<IHttpRequest> Req = FHttpModule::Get().CreateRequest();
	Req->SetURL(ServerUrl + TEXT("/api/talk"));
	Req->SetVerb(TEXT("POST"));
	Req->SetHeader(TEXT("Content-Type"), TEXT("application/json"));

	// 组装 JSON：{"npc_id": .., "message": .., "voice": ..}
	FString Esc = Message.Replace(TEXT("\"), TEXT("\\"))
	                   .Replace(TEXT("\""), TEXT("\\""));
	FString Body = FString::Printf(TEXT("{\"npc_id\":\"%s\",\"message\":\"%s\"%s}"),
		*NpcId, *Esc, bVoice ? TEXT(",\"voice\":true") : TEXT(""));
	Req->SetContentAsString(Body);

	Req->OnProcessRequestComplete().BindLambda([this](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		if (!bOk || !Resp.IsValid() || Resp->GetResponseCode() != 200)
			return;
		TSharedPtr<FJsonObject> Json;
		if (!FJsonSerializer::Deserialize(
			TJsonReaderFactory<>::Create(Resp->GetContentAsString()), Json) || !Json.IsValid())
			return;
		if (Json->HasField(TEXT("reply")))
			OnReply.Broadcast(Json->GetStringField(TEXT("reply")));
		if (bVoice && Json->HasField(TEXT("audio")))
		{
			const FString B64 = Json->GetStringField(TEXT("audio"));
			if (!B64.IsEmpty())
			{
				TArray<uint8> Bytes;
				FBase64::Decode(B64, Bytes);
				OnAudio.Broadcast(Bytes);
			}
		}
	});
	Req->ProcessRequest();
}

void UNpcBrain::PollState()
{
	TSharedRef<IHttpRequest> Req = FHttpModule::Get().CreateRequest();
	Req->SetURL(ServerUrl + TEXT("/api/state"));
	Req->SetVerb(TEXT("GET"));
	Req->OnProcessRequestComplete().BindLambda([this](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		if (!bOk || !Resp.IsValid() || Resp->GetResponseCode() != 200)
			return;
		TSharedPtr<FJsonObject> Json;
		if (!FJsonSerializer::Deserialize(
			TJsonReaderFactory<>::Create(Resp->GetContentAsString()), Json) || !Json.IsValid())
			return;
		const TSharedPtr<FJsonObject>* Actors = nullptr;
		if (Json->TryGetObjectField(TEXT("actors"), Actors))
		{
			const TSharedPtr<FJsonObject>* Me = nullptr;
			if (Actors->Get()->TryGetObjectField(NpcId, Me))
			{
				Position = Me.Get()->Get()->GetStringField(TEXT("position"));
				State = Me.Get()->Get()->GetStringField(TEXT("state"));
			}
		}
	});
	Req->ProcessRequest();
}

void UNpcBrain::TickComponent(float DeltaTime, ELevelTick TickType,
	FActorComponentTickFunction* ThisTickFunction)
{
	Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
	PollTimer += DeltaTime;
	if (PollTimer >= 2.f) { PollTimer = 0.f; PollState(); }
}
