/**
 * Google Apps Script: Quiz Form Generator
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    if (data.action === "close") {
      return handleClose(data);
    }
    return handleCreate(data);
  } catch (err) {
    console.error("doPost failed: " + err.toString());
    return ContentService.createTextOutput(JSON.stringify({ error: err.toString() }))
                         .setMimeType(ContentService.MimeType.JSON);
  }
}

function handleCreate(data) {
  var formTitle = data.title || "Quiz";
  var form = FormApp.create(formTitle);
  var formId = form.getId();

  form.setIsQuiz(true);
  form.setCollectEmail(true);

  var questions = data.questions || [];
  for (var i = 0; i < questions.length; i++) {
    var q = questions[i];
    var item = form.addMultipleChoiceItem();
    item.setTitle(q.question);
    item.setPoints(1);

    var choices = [];
    var options = q.options || [];
    for (var j = 0; j < options.length; j++) {
      choices.push(item.createChoice(options[j], options[j] === q.correct_answer));
    }
    item.setChoices(choices);
  }

  if (!data.callback_url) {
    throw new Error("callback_url is required");
  }
  PropertiesService.getScriptProperties().setProperty("callback_" + formId, data.callback_url);

  ScriptApp.newTrigger("onFormSubmit")
    .forForm(form)
    .onFormSubmit()
    .create();

  console.log("Form created: " + formId + ", callback: " + data.callback_url);

  return ContentService
    .createTextOutput(JSON.stringify({ formUrl: form.getPublishedUrl(), formId: formId }))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleClose(data) {
  var formId = data.id;
  if (!formId) {
    return ContentService.createTextOutput(JSON.stringify({ error: "id required" }))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var form = FormApp.openById(formId);
  form.setAcceptingResponses(false);

  PropertiesService.getScriptProperties().deleteProperty("callback_" + formId);

  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getTriggerSourceId() === formId) {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }

  return ContentService.createTextOutput(JSON.stringify({ status: "closed", id: formId }))
                       .setMimeType(ContentService.MimeType.JSON);
}

function onFormSubmit(e) {
  try {
    var formResponse = e.response;
    var formId = e.source.getId();

    // Calculate score manually — getScore() may not be ready when trigger fires
    var totalScore = 0;
    var maxScore = 0;
    var itemResponses = formResponse.getItemResponses();
    for (var i = 0; i < itemResponses.length; i++) {
      var itemResponse = itemResponses[i];
      var item = itemResponse.getItem();
      if (item.getType() !== FormApp.ItemType.MULTIPLE_CHOICE) continue;

      var mcItem = item.asMultipleChoiceItem();
      maxScore += mcItem.getPoints();

      var correctChoices = mcItem.getChoices().filter(function(c) { return c.isCorrectAnswer(); });
      var correctAnswer = correctChoices.length > 0 ? correctChoices[0].getValue() : null;
      if (correctAnswer && itemResponse.getResponse() === correctAnswer) {
        totalScore += mcItem.getPoints();
      }
    }

    var callbackUrl = PropertiesService.getScriptProperties().getProperty("callback_" + formId);
    if (!callbackUrl) {
      throw new Error("No callback URL found for form " + formId);
    }

    var payload = {
      student_email: formResponse.getRespondentEmail(),
      score: totalScore,
      max_score: maxScore,
      form_id: formId,
    };

    var response = UrlFetchApp.fetch(callbackUrl, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });

    console.log("Callback sent: " + response.getResponseCode() + " score=" + totalScore + "/" + maxScore);

    var triggers = ScriptApp.getProjectTriggers();
    for (var t = 0; t < triggers.length; t++) {
      if (triggers[t].getTriggerSourceId() === formId) {
        ScriptApp.deleteTrigger(triggers[t]);
      }
    }
  } catch (err) {
    console.error("onFormSubmit failed: " + err.toString());
  }
}
