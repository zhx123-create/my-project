$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$stream = $null
$random = $null
$bitmap = $null
$stage = 'runtime'
try {
    # Drain the pipe before native OCR initialization, so the parent can enforce
    # its timeout even if a Windows component stalls during initialization.
    $inputData = [Console]::In.ReadToEnd()
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    if ($null -eq $asTask) { throw 'runtime' }
    function Await-Operation($operation, $resultType) {
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        $task.Wait()
        return $task.Result
    }
    $engine = $null
    $chinese = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
    if ($null -ne $chinese) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($chinese) }
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) {
        [Console]::Error.Write('{"ocr_error":"language"}')
        exit 2
    }
    $bytes = [Convert]::FromBase64String($inputData)
    $inputData = $null
    $stage = 'decode'
    $stream = New-Object System.IO.MemoryStream(, $bytes)
    $random = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
    $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($random)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $stage = 'failed'
    $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    $output = @{ text = ($lines -join "`n"); count = $lines.Count; language = $engine.RecognizerLanguage.LanguageTag }
    [Console]::Out.Write(($output | ConvertTo-Json -Compress))
    exit 0
} catch {
    [Console]::Error.Write(('{"ocr_error":"' + $stage + '"}'))
    exit 1
} finally {
    foreach ($resource in @($bitmap, $random, $stream)) {
        if ($resource -is [System.IDisposable]) {
            try { $resource.Dispose() } catch {}
        }
    }
}
