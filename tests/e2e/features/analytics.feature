Feature: Analytics Dashboard
  As a logged-in admin
  I want to view the analytics dashboard
  So that I can monitor student progress and submission statistics

  Background:
    Given the teacher account exists in the database
    And I am logged in as the admin
    And the E2E test subject exists

  @smoke @analytics
  Scenario: Admin views analytics dashboard without errors
    When I navigate to the analytics page
    Then the analytics page should load without errors
    And statistical content should be visible on the page

  @analytics
  Scenario: Admin views fraud detection analytics
    When I navigate to the fraud analytics page
    Then the fraud analytics page should load without errors
